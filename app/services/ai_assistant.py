"""The WomanUP AI Assistant: education, health and the daily message.

Distinct from the navigator. The navigator answers questions *about the portal*
from approved sources; the assistant advises the woman herself, and its whole
value is that the answer differs per person. That makes the persona — age band,
interests, goals, direction, progress — the core of this module rather than a
decoration.

Three boundaries are enforced here, before any model call:

* **Safety.** A disclosure of violence or self-harm goes to a human, never to
  the model. Same filter and same ordering as the navigator.
* **Age.** The portal serves girls from 10, so adult health topics are refused
  structurally for minors — see `age_gate`. An unknown age is treated as a
  minor.
* **Consent.** Personalising on profile data requires the user's
  `ai_personalisation` consent. Without it the assistant still answers, but
  from an empty persona and it says so — the answer degrades, the consent gate
  does not bend.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    AgeBand,
    AssistantSection,
    ConsentScope,
    EnrollmentStatus,
    normalise_language,
)
from app.models.assessment import DevelopmentScore
from app.models.audit import AiInteraction
from app.models.plan import DevelopmentPlan
from app.models.profile import Goal, Profile
from app.models.program import Enrollment, Program
from app.models.user import User
from app.services import ai_navigator, conversation, rag, safety
from app.services import skills as skill_service
from app.services.age_gate import (
    HEALTH_SCOPE,
    age_from_profile,
    band_for_profile,
    blocks_adult_health,
    is_minor,
)
from app.services.audit_service import has_consent
from app.services.diagnostic import level_for
from app.services.llm_gateway import LlmResponse, LlmUnavailableError, llm_gateway
from app.services.prompts import (
    ASSISTANT_DAILY_SCHEMA,
    ASSISTANT_DAILY_SYSTEM,
    ASSISTANT_DETAIL_SCHEMA,
    ASSISTANT_DETAIL_SYSTEM,
    ASSISTANT_EDUCATION_SCHEMA,
    ASSISTANT_EDUCATION_SYSTEM,
    ASSISTANT_HEALTH_SCHEMA,
    ASSISTANT_HEALTH_SYSTEM,
    ASSISTANT_ROUTER_SCHEMA,
    ASSISTANT_ROUTER_SYSTEM,
)
from app.services.scoring import composite_score, distinctly_weakest
from app.services.uzbek import fold, question_language, to_new_alphabet

logger = logging.getLogger(__name__)

# How many questions someone gets before signing up. Deliberately enough to see
# what the assistant is, and not enough to use it as a free service.
GUEST_QUESTION_ALLOWANCE = 3


class GuestLimitReached(RuntimeError):
    """The pre-registration allowance is spent."""


@dataclass(slots=True)
class Persona:
    """What the assistant knows about the woman it is answering.

    Everything here comes from her own profile and is only populated once she
    has consented to AI personalisation.
    """

    band: AgeBand = AgeBand.UNKNOWN
    age: int | None = None
    name: str | None = None
    language: str = "uz"
    interests: list[str] = field(default_factory=list)
    #: What the platform records that she can do, from the canonical skill
    #: layer — not the free text she once typed into her profile. Each note
    #: carries how well the skill is known, because "learned" and "verified"
    #: are different claims and the assistant must never merge them.
    skills: list[skill_service.SkillNote] = field(default_factory=list)
    education_level: str | None = None
    profession: str | None = None
    employment_status: str | None = None
    region: str | None = None
    goals: list[str] = field(default_factory=list)
    directions: list[str] = field(default_factory=list)
    in_progress: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    plan_progress: int | None = None
    #: Whether she has a Development Score at all, and its level key
    #: (`good_foundation` …) — the same words the result page uses, so the
    #: assistant cannot call a "good foundation" "developing".
    assessed: bool = False
    composite: int | None = None
    level: str | None = None
    personalised: bool = False
    #: Signed in, whether or not she consented to personalisation. A woman who
    #: is signed in must never be told to sign in (C-28).
    signed_in: bool = False

    @property
    def is_minor(self) -> bool:
        return is_minor(self.band)

    def as_prompt(self) -> str:
        """The persona block handed to the model. No direct identifiers."""
        if not self.personalised and self.signed_in:
            return (
                "READER PROFILE: not available — she IS signed in, but has not "
                "turned on AI personalisation, so you may not use her profile. "
                "Answer generally. Never ask her to sign in or to create an "
                "account. You may mention once that she can turn on "
                'personalisation in the assistant settings ("Yordamchini '
                'sozlash") to get advice specific to her.'
            )
        if not self.personalised:
            return (
                "READER PROFILE: not available — she is a guest who has not "
                "signed in. Answer generally, and mention once that signing up "
                "and completing her profile makes the advice specific to her."
            )

        lines = [
            "READER PROFILE:",
            f"- age band: {self.band.value}" + (f" ({self.age} years)" if self.age else ""),
            f"- language: {self.language}",
        ]
        if self.education_level:
            lines.append(f"- education: {self.education_level}")
        if self.profession:
            lines.append(f"- current occupation: {self.profession}")
        if self.employment_status:
            lines.append(f"- employment: {self.employment_status}")
        if self.region:
            lines.append(f"- region: {self.region}")
        if self.interests:
            lines.append(
                f"- interests (what she likes — NOT skills she has): {', '.join(self.interests)}"
            )
        if self.skills:
            lines.append(
                "- skills WomanUP records for her (name — how well it is known — level): "
                + "; ".join(note.as_line() for note in self.skills)
            )
            lines.append(
                "  a skill is *learned* when a course taught it, *assessed* when an "
                "assessment scored it, and *verified* only when a mentor, an employer, "
                "an internship or a job result confirmed it. Finishing a course never "
                "makes a skill verified — never tell her it does."
            )
        if self.goals:
            lines.append(f"- her stated goals: {'; '.join(self.goals)}")
        if self.level and self.composite is not None:
            label = _LEVEL_LABEL.get(self.level, {}).get(self.language) or self.level
            lines.append(
                f"- overall Development Score: {self.composite}/100, level «{label}» — "
                "use exactly this level name if you mention it"
            )
        if self.directions:
            lines.append(f"- weakest development dimensions: {', '.join(self.directions)}")
        elif self.assessed:
            lines.append(
                "- development dimensions: all at a similar level — do not name any of "
                "them as her weakest"
            )
        if self.in_progress:
            lines.append(f"- studying now: {', '.join(self.in_progress)}")
        if self.completed:
            lines.append(f"- already completed: {', '.join(self.completed)}")
        if self.plan_progress is not None:
            lines.append(f"- development plan progress: {self.plan_progress}%")
        return "\n".join(lines)


@dataclass(slots=True)
class AssistantAnswer:
    """One reply, with the trace fields section 06 requires."""

    answer: str
    section: AssistantSection
    trace_id: str
    personalised: bool
    kind: str = "general"
    escalated: bool = False
    escalation_reason: str | None = None
    see_a_doctor: bool = False
    professions: list[dict] = field(default_factory=list)
    roadmap: list[dict] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    programs: list[dict] = field(default_factory=list)
    # Populated when the question was answered from the approved knowledge base
    # rather than generated: the portal's own documentation, quoted with its
    # sources, is a different kind of answer and should look like one.
    sources: list[dict] = field(default_factory=list)
    confidence: float | None = None
    is_uncertain: bool = False
    route: str = "education"
    guest_questions_left: int | None = None
    #: Approved help lines, set only on a safety reply — rendered as a
    #: separate call card rather than buried in the text.
    resources: list[dict] = field(default_factory=list)
    #: Composed by rules or quoted because the model was unavailable.
    degraded: bool = False


async def build_persona(
    session: AsyncSession,
    user_id: uuid.UUID | None,
    *,
    language: str | None = None,
) -> Persona:
    """Assemble the persona, or an empty one when we may not personalise.

    `language` is the locale the request was made in and wins over the stored
    column. Resolving it here rather than at each answer site is deliberate:
    every canned reply in this module — unavailable, escalation, out-of-scope —
    already keys off `persona.language`, so one override fixes all of them at
    once and cannot be forgotten at a call site added later.
    """
    if user_id is None:
        return Persona(language=normalise_language(language) or "uz")

    user = await session.get(User, user_id)
    language = (
        normalise_language(language)
        or (user.language.value if user and user.language else None)
        or "uz"
    )

    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    band = band_for_profile(profile)

    # The age band is a safety input, so it is derived even without consent —
    # it decides what may be *withheld*, never what is disclosed to anyone.
    if not await has_consent(session, user_id, ConsentScope.AI_PERSONALISATION):
        return Persona(band=band, language=language, signed_in=True)

    goals = list(
        (
            await session.execute(
                select(Goal).where(Goal.user_id == user_id, Goal.achieved.is_(False)).limit(5)
            )
        ).scalars()
    )
    scores = {
        row.dimension: row.current
        for row in (
            await session.execute(
                select(DevelopmentScore).where(DevelopmentScore.user_id == user_id)
            )
        ).scalars()
    }
    enrolments = list(
        (
            await session.execute(
                select(Enrollment, Program)
                .join(Program, Program.id == Enrollment.program_id)
                .where(Enrollment.user_id == user_id)
                .limit(20)
            )
        ).all()
    )
    plan = await session.scalar(
        select(DevelopmentPlan).where(
            DevelopmentPlan.user_id == user_id, DevelopmentPlan.is_active.is_(True)
        )
    )

    def title(program: Program) -> str:
        return (
            program.title_i18n.get(language)
            or program.title_i18n.get("uz")
            or next(iter(program.title_i18n.values()), "")
        )

    return Persona(
        band=band,
        age=age_from_profile(profile),
        name=(profile.full_name.split()[0] if profile and profile.full_name else None),
        language=language,
        interests=list(profile.interests) if profile else [],
        skills=await skill_service.notes_for(session, user_id, language=language),
        education_level=profile.education_level if profile else None,
        profession=profile.profession if profile else None,
        employment_status=profile.employment_status if profile else None,
        region=user.region.value if user and user.region else None,
        goals=[g.title for g in goals],
        directions=[d.value for d in distinctly_weakest(scores, limit=3)],
        in_progress=[title(p) for e, p in enrolments if e.status is EnrollmentStatus.IN_PROGRESS],
        completed=[title(p) for e, p in enrolments if e.status is EnrollmentStatus.COMPLETED],
        plan_progress=plan.progress_percent if plan else None,
        assessed=bool(scores),
        composite=round(composite_score(scores)) if scores else None,
        level=level_for(composite_score(scores)) if scores else None,
        personalised=True,
        signed_in=True,
    )


def _title(program: Program, language: str) -> str:
    return (
        program.title_i18n.get(language)
        or program.title_i18n.get("uz")
        or next(iter(program.title_i18n.values()), "")
    )


def _ref(program: Program, language: str) -> dict:
    return {
        "id": str(program.id),
        "slug": program.slug,
        "title": _title(program, language),
        "category": program.category.value,
    }


#: Words of a catalogue *question* that say nothing about the subject: "which
#: courses are there on the portal". Compared as `_key` writes them.
_ASK_FILLER = frozenset(
    _word
    for _word in (  # noqa: SIM905
        "portal portalda portalida portalning platforma womanup qaysi qanday qanaqa bormi "
        "menga mening kerak nima organmoqchiman organish oqish istayman kurs kurslar "
        "kursi kursga dastur dasturlar dasturi bilan uchun haqida yordam bering bolmoqchiman "
        "какие какой какая каких есть ли портале портал платформе хочу научиться мне курсы "
        "курс курса курсов программы программа посоветуйте помогите для как что где "
        "which what there help become give links want would like please recommend learn "
        "study need course courses programme programmes program programs the and for with "
        "can are any portal"
    ).split()
)
_WORD_RE = re.compile(r"[^\W\d_][^\W_]*", re.UNICODE)
_KEY_LENGTH = 6


def _key(word: str) -> str:
    """A word as the catalogue match compares it: folded, old spelling, and cut
    to six letters so "программированию" meets "Программирование" and
    "tikuvçilikni" meets "Tikuvçilik"."""
    folded = fold(skill_service.normalise(word))
    return folded[:_KEY_LENGTH]


def _keys(text: str) -> set[str]:
    return {_key(w) for w in _WORD_RE.findall(text or "") if len(w) >= 3}


CATALOGUE_LIMIT = 1500
#: A title word is the strong signal; a taught skill next; the description is
#: only a tie-breaker, so a long text full of common words cannot outvote it.
_TITLE_WEIGHT, _SKILL_WEIGHT, _TAXONOMY_WEIGHT, _DESCRIPTION_WEIGHT = 2.0, 1.5, 3.0, 0.3
_MIN_MATCH = 1.5


async def catalogue_matches(session: AsyncSession, text: str, *, limit: int = 6) -> list[Program]:
    """Published programmes that match what she asked, best first.

    Deterministic and model-free — her question's words against each
    programme's title, taught skills (also through the skill taxonomy, which
    joins "graphic design" with "графический дизайн") and, faintly, its
    description — so it also serves as the degraded answer when the model is
    down.
    """
    words = [
        w
        for w in _WORD_RE.findall(text or "")
        if len(w) >= 3 and fold(skill_service.normalise(w)) not in _ASK_FILLER
    ]
    wanted = {_key(w) for w in words}
    if not wanted:
        return []
    labels = [*words, *(f"{a} {b}" for a, b in zip(words, words[1:], strict=False))]
    index = await skill_service.SkillIndex.load(session, labels)
    wanted_skills = {index.key(label) for label in labels if index.get(label) is not None}

    programs = (
        await session.execute(
            select(Program)
            .where(Program.is_published.is_(True))
            .order_by(Program.published_at.desc().nullslast())
            .limit(CATALOGUE_LIMIT)
        )
    ).scalars()
    scored: list[tuple[float, str, Program]] = []
    for program in programs:
        title = " ".join(str(v) for v in (program.title_i18n or {}).values() if v)
        taught = [label for label in (program.skills_taught or []) if label]
        score = _TITLE_WEIGHT * len(wanted & _keys(title))
        score += _SKILL_WEIGHT * len(wanted & _keys(" ".join(taught)))
        if wanted_skills:
            score += _TAXONOMY_WEIGHT * len(wanted_skills & {index.key(t) for t in taught})
        description = " ".join(str(v) for v in (program.description_i18n or {}).values() if v)
        score += min(1.0, _DESCRIPTION_WEIGHT * len(wanted & _keys(description[:2000])))
        if score >= _MIN_MATCH:
            scored.append((score, title, program))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [program for _, _, program in scored[:limit]]


async def _catalogue(
    session: AsyncSession, language: str, *, matches: list[Program] | None = None
) -> tuple[str, dict[str, dict]]:
    """The programmes the model may cite: what matched her question first,
    then a slice of the rest so a roadmap step can still find a course."""
    chosen: list[Program] = list(matches or [])
    seen = {program.id for program in chosen}
    rest = (
        await session.execute(
            select(Program)
            .where(Program.is_published.is_(True))
            .order_by(Program.published_at.desc().nullslast())
            .limit(60)
        )
    ).scalars()
    chosen += [program for program in rest if program.id not in seen][: max(0, 60 - len(chosen))]
    lines: list[str] = []
    index: dict[str, dict] = {}
    for program in chosen:
        ref = _ref(program, language)
        lines.append(f"- {program.id} | {ref['category']} | {ref['title']}")
        index[ref["id"]] = ref
    return "\n".join(lines) or "- (catalogue empty)", index


def _matches_block(matches: list[Program], language: str) -> str:
    if not matches:
        return ""
    lines = [
        "PORTAL PROGRAMMES THAT MATCH HER QUESTION (title | category) — real courses "
        "she can enrol in; name them where they fit:"
    ]
    lines += [f"- {_title(p, language)} | {p.category.value}" for p in matches]
    return "\n".join(lines) + "\n\n"


def _answer_language(question: str, language: str | None) -> str | None:
    """The language to answer in: the question's own when it is clear, else
    the interface locale the request was made in."""
    return question_language(question) or language


async def ask_education(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    question: str,
    language: str | None = None,
    persona: Persona | None = None,
    gateway=None,
    history: list | None = None,
) -> AssistantAnswer:
    """The conversational half of the education answer.

    Deliberately small: the detailed profession and roadmap cards are a second
    pass. Generation runs at roughly 40 tokens a second, so folding the cards
    into this call pushed it past fifty seconds — long enough that the page
    reads as broken. Split, the reply lands in about ten and the cards arrive
    behind it.
    """
    gateway = gateway or llm_gateway
    persona = persona or await build_persona(
        session, user_id, language=_answer_language(question, language)
    )
    lang = persona.language

    if rag.needs_human_escalation(question):
        return await _escalate(session, user_id, AssistantSection.EDUCATION, persona, question)

    turns = conversation.clean(history)
    # Real programmes that match what she asked — the prompt used to promise
    # a catalogue it never sent, and the model told her so (B-09, B-14).
    matches = await catalogue_matches(
        session, f"{conversation.last_user_text(turns)} {question}" if turns else question
    )

    content = (
        f"{persona.as_prompt()}\n\n"
        f"{conversation.as_prompt(turns)}"
        f"{_matches_block(matches, lang)}"
        f"ANSWER LANGUAGE: {lang}\nQUESTION: {gateway.sanitise(question)}"
    )

    payload, response = await _complete(
        gateway,
        system=ASSISTANT_EDUCATION_SYSTEM,
        content=content,
        schema=ASSISTANT_EDUCATION_SCHEMA,
        max_tokens=8000,
        effort="low",
    )
    if payload is None:
        # Degrade, don't fail: the catalogue search needs no model.
        answer = _degraded_education(persona, matches, response)
        await _record(
            session,
            user_id,
            AssistantSection.EDUCATION,
            question,
            answer,
            response,
            model=None if response else "unavailable",
            prompt_version="education-fallback",
        )
        return answer

    answer = AssistantAnswer(
        answer=payload.get("answer", ""),
        section=AssistantSection.EDUCATION,
        trace_id=response.trace_id,
        personalised=persona.personalised,
        kind=payload.get("kind", "general"),
        next_actions=list(payload.get("next_actions", []))[:3],
        programs=[_ref(p, lang) for p in matches[:4]],
    )
    await _record(session, user_id, AssistantSection.EDUCATION, question, answer, response)
    return answer


async def education_detail(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    question: str,
    answer_so_far: str,
    kind: str = "general",
    language: str | None = None,
    persona: Persona | None = None,
    gateway=None,
    history: list | None = None,
) -> AssistantAnswer:
    """The cards: professions or an ordered roadmap, tied to real programmes."""
    gateway = gateway or llm_gateway
    persona = persona or await build_persona(
        session, user_id, language=_answer_language(question, language)
    )
    lang = persona.language

    # Both strings come from the client and both would reach the prompt, so
    # both pass the safety gate before anything else (I-03).
    if safety.is_disclosure(question, answer_so_far):
        return await _escalate(
            session, user_id, AssistantSection.EDUCATION, persona, f"{question}\n{answer_so_far}"
        )

    turns = conversation.clean(history)
    matches = await catalogue_matches(session, f"{question} {answer_so_far[:600]}", limit=12)
    catalogue, index = await _catalogue(session, lang, matches=matches)
    content = (
        f"{persona.as_prompt()}\n\n"
        f"{conversation.as_prompt(turns)}"
        f"PORTAL PROGRAMME CATALOGUE (id | category | title):\n{catalogue}\n\n"
        f"ANSWER LANGUAGE: {lang}\n"
        f"REQUEST KIND: {kind}\n"
        f"QUESTION: {gateway.sanitise(question)}\n"
        f"ANSWER ALREADY GIVEN: {gateway.sanitise(answer_so_far[:1200])}"
    )

    payload, response = await _complete(
        gateway,
        system=ASSISTANT_DETAIL_SYSTEM,
        content=content,
        schema=ASSISTANT_DETAIL_SCHEMA,
        max_tokens=16000,
        effort="low",
    )
    if payload is None:
        # No cards without a model — but the matching courses are still real.
        answer = AssistantAnswer(
            answer="",
            section=AssistantSection.EDUCATION,
            trace_id=response.trace_id if response else uuid.uuid4().hex,
            personalised=persona.personalised,
            kind=kind,
            programs=[_ref(p, lang) for p in matches[:6]],
            escalation_reason="model_unavailable",
            degraded=True,
        )
        await _record(
            session,
            user_id,
            AssistantSection.EDUCATION,
            question,
            answer,
            response,
            feature="assistant_education_detail",
            model=None if response else "unavailable",
            prompt_version="detail-fallback",
        )
        return answer

    # Programme ids only count if they exist — a hallucinated id would render
    # as a broken enrol link.
    referenced: list[dict] = []
    seen: set[str] = set()
    for block in (*payload.get("professions", []), *payload.get("roadmap", [])):
        for program_id in block.get("program_ids", []) or []:
            entry = index.get(str(program_id))
            if entry and entry["id"] not in seen:
                seen.add(entry["id"])
                referenced.append(entry)

    answer = AssistantAnswer(
        answer="",
        section=AssistantSection.EDUCATION,
        trace_id=response.trace_id,
        personalised=persona.personalised,
        kind=kind,
        professions=list(payload.get("professions", []))[:3],
        roadmap=list(payload.get("roadmap", []))[:7],
        programs=referenced[:6],
    )
    # The cards are the most visible AI content on the page; they leave a
    # trace like every other answer (I-04). The excerpt is the card titles.
    await _record(
        session,
        user_id,
        AssistantSection.EDUCATION,
        question,
        answer,
        response,
        feature="assistant_education_detail",
        response_text=" · ".join(
            str(block.get("title", "")) for block in (*answer.professions, *answer.roadmap)
        ),
    )
    return answer


async def ask_health(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    question: str,
    language: str | None = None,
    persona: Persona | None = None,
    gateway=None,
    history: list | None = None,
) -> AssistantAnswer:
    """Age-appropriate health information. Never a diagnosis."""
    gateway = gateway or llm_gateway
    persona = persona or await build_persona(
        session, user_id, language=_answer_language(question, language)
    )
    lang = persona.language

    if rag.needs_human_escalation(question):
        return await _escalate(session, user_id, AssistantSection.HEALTH, persona, question)

    # The gate, before the model. An adult topic from a minor is not answered
    # by a generative system at all — it is redirected to a person.
    if blocks_adult_health(question, persona.band):
        answer = AssistantAnswer(
            answer=_OUT_OF_SCOPE[lang if lang in _OUT_OF_SCOPE else "uz"],
            section=AssistantSection.HEALTH,
            trace_id=uuid.uuid4().hex,
            personalised=persona.personalised,
            escalated=True,
            escalation_reason="age_restricted_topic",
            see_a_doctor=True,
        )
        await _record(
            session,
            user_id,
            AssistantSection.HEALTH,
            question,
            answer,
            None,
            model="age-gate",
            prompt_version="age-gate",
        )
        return answer

    turns = conversation.clean(history)
    system = ASSISTANT_HEALTH_SYSTEM.format(scope=HEALTH_SCOPE[persona.band])
    content = (
        f"{persona.as_prompt()}\n\n"
        f"{conversation.as_prompt(turns)}"
        f"ANSWER LANGUAGE: {lang}\nQUESTION: {gateway.sanitise(question)}"
    )

    payload, response = await _complete(
        gateway,
        system=system,
        content=content,
        schema=ASSISTANT_HEALTH_SCHEMA,
        max_tokens=8000,
        effort="low",
    )
    if payload is None:
        answer = _unavailable(AssistantSection.HEALTH, persona, response)
        await _record(
            session,
            user_id,
            AssistantSection.HEALTH,
            question,
            answer,
            response,
            model=None if response else "unavailable",
            prompt_version="health-fallback",
        )
        return answer

    answer = AssistantAnswer(
        answer=payload.get("answer", ""),
        section=AssistantSection.HEALTH,
        trace_id=response.trace_id,
        personalised=persona.personalised,
        see_a_doctor=bool(payload.get("see_a_doctor")),
        escalated=bool(payload.get("out_of_scope")),
        escalation_reason="out_of_scope" if payload.get("out_of_scope") else None,
        next_actions=list(payload.get("next_actions", []))[:4],
    )
    await _record(session, user_id, AssistantSection.HEALTH, question, answer, response)
    return answer


async def daily_message(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    language: str | None = None,
    persona: Persona | None = None,
    gateway=None,
    today: date | None = None,
) -> AssistantAnswer:
    """One short line for today, built from her own profile.

    Cached for the day: the same woman opening the app twice should see the
    same message, and a fresh model call per page load would be waste.
    """
    gateway = gateway or llm_gateway
    persona = persona or await build_persona(session, user_id, language=language)
    lang = language or persona.language
    today = today or datetime.now(UTC).date()

    cached = await session.scalar(
        select(AiInteraction)
        .where(
            AiInteraction.user_id == user_id,
            AiInteraction.feature == _feature(AssistantSection.DAILY),
            AiInteraction.prompt_excerpt == f"daily:{lang}",
            AiInteraction.created_at >= datetime.combine(today, datetime.min.time(), tzinfo=UTC),
        )
        .order_by(AiInteraction.created_at.desc())
        .limit(1)
    )
    if cached is not None and cached.response_excerpt:
        return AssistantAnswer(
            answer=cached.response_excerpt,
            section=AssistantSection.DAILY,
            trace_id=cached.trace_id or uuid.uuid4().hex,
            personalised=persona.personalised,
        )

    content = (
        f"{persona.as_prompt()}\n\n"
        f"ANSWER LANGUAGE: {lang}\n"
        f"DATE: {today.isoformat()}\n"
        "Write today's message."
    )
    payload, response = await _complete(
        gateway,
        system=ASSISTANT_DAILY_SYSTEM,
        content=content,
        schema=ASSISTANT_DAILY_SCHEMA,
        max_tokens=4000,
        effort="low",
    )
    if payload is None:
        answer = _unavailable(AssistantSection.DAILY, persona, response)
        # Traced, but under a key the cache never reads.
        await _record(
            session,
            user_id,
            AssistantSection.DAILY,
            f"daily-failed:{lang}",
            answer,
            response,
            model=None if response else "unavailable",
            prompt_version="daily-fallback",
        )
        return answer

    message = payload.get("message", "")
    if lang == "uz":
        message = to_new_alphabet(message)
    answer = AssistantAnswer(
        answer=message,
        section=AssistantSection.DAILY,
        trace_id=response.trace_id,
        personalised=persona.personalised,
    )
    # A line that still mixes scripts after repair is shown once, but never
    # cached for the whole day (G-14).
    key = f"daily:{lang}" if _script_ok(message, lang) else f"daily-unchecked:{lang}"
    await _record(session, user_id, AssistantSection.DAILY, key, answer, response)
    return answer


def _script_ok(text: str, language: str) -> bool:
    """Whether a reply is written in its language's script throughout."""
    if language in {"uz", "en"}:
        return not re.search("[Ѐ-ӿ]", text)
    return True


# --------------------------------------------------------------- the one door
#
# Routing used to be a model call on every question: two to three seconds and
# an untraced, unsanitised prompt (H-09, I-04). Most questions name their
# subject plainly, so a keyword router decides those; the model is asked only
# when the words are ambiguous, and that call is sanitised and traced.

_ROUTE_WORDS: dict[str, tuple[str, ...]] = {
    # Written as `uzbek.fold` writes text: lower case, old digraphs, no marks.
    "portal": (
        "sertifikat", "сертификат", "certificate",
        "royxatdan", "регистрац", "register", "sign up", "signup",
        "parol", "пароль", "password", "akkaunt", "аккаунт", "account",
        "profil", "профил", "profile", "kabinet", "кабинет",
        "diagnostika", "диагностик", "diagnostic", "development score",
        "rivojlanish reja", "план развития", "development plan",
        "rozilik", "согласи", "consent", "malumotlarim", "shaxsiy malumot",
        "персональн", "personal data", "privacy", "конфиденц",
        "ariza", "заявк", "application", "apply",
        "mentor", "ментор", "yozilaman", "yozilish", "yozilsam",
        "записат", "enrol", "womanup", "portal", "портал", "platforma",
        "платформ", "platform",
    ),
    "health": (
        "sogliq", "salomat", "здоров", "health", "uyqu", "сон", "sleep",
        "ovqat", "питан", "nutrition", "diet", "stress", "стресс",
        "kayfiyat", "настроени", "mood", "hayz", "менстру", "period pain",
        "gigiyena", "гигиен", "hygiene", "jismoniy", "mashq", "упражн",
        "exercise", "homilador", "беремен", "pregnan", "bosh ogri",
        "головн", "headache", "charchoq", "устал", "tired", "vazn",
        "weight", "psixolog", "anxiety", "тревог", "xavotir", "osmirlik",
        "puberty", "tana", "body",
    ),
    "education": (
        "kasb", "профес", "profession", "career", "karyera", "карьер",
        "ish top", "ishga kir", "работ", "job", "organ", "учить", "изуч",
        "learn", "study", "oqish", "kurs", "курс", "course", "dastur",
        "hunar", "ремесл", "craft", "tikuv", "шить", "sew", "dizayn",
        "дизайн", "design", "программ", "programming", "biznes",
        "бизнес", "business", "ingliz", "англ", "english", "rezyume",
        "резюме", "resume", "suhbat", "собеседован", "interview",
        "buxgalter", "бухгалтер", "accountant", "marketing", "маркетинг",
    ),
}  # fmt: skip

#: Words that make a question about the *catalogue*: "which courses are there".
#: Those go to education, which is now handed the matching programmes.
_COURSE_WORDS = ("kurs", "курс", "course", "dastur", "programme", "program ")
#: Portal words a catalogue question carries without being about the portal.
_SITE_WORDS = ("womanup", "portal", "портал", "platforma", "платформ", "platform")


def _hits(folded: str, words: tuple[str, ...]) -> int:
    return sum(1 for word in words if re.search(r"(?<!\w)" + re.escape(word), folded))


def keyword_route(question: str) -> str | None:
    """The route the words make obvious, or None when they do not."""
    folded = fold(question)
    scores = {route: _hits(folded, words) for route, words in _ROUTE_WORDS.items()}
    if _hits(folded, _COURSE_WORDS):
        # "Is there a sewing course on the portal" is a catalogue question:
        # the site name does not make it a documentation question.
        scores["portal"] -= _hits(folded, _SITE_WORDS)
        scores["education"] += 1
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (best, top), (_, second) = ranked[0], ranked[1]
    if top >= 1 and top > second:
        return best
    return None


async def classify(
    question: str,
    gateway=None,
    *,
    session: AsyncSession | None = None,
    user_id: uuid.UUID | None = None,
    history: list | None = None,
) -> str:
    """Which capability should answer this.

    Keywords first; for a bare follow-up, her previous question's keywords;
    the model only when both are ambiguous; education when nothing decides.
    """
    gateway = gateway or llm_gateway
    turns = conversation.clean(history)
    route = keyword_route(question)
    if route is None and turns:
        route = keyword_route(f"{conversation.last_user_text(turns)} {question}")
    if route is not None:
        return route
    if not gateway.enabled:
        return "education"

    previous = conversation.last_user_text(turns)
    content = (
        f"PREVIOUS QUESTION: {previous}\n" if previous else ""
    ) + f"QUESTION: {gateway.sanitise(question)}"
    payload, response = await _complete(
        gateway,
        system=ASSISTANT_ROUTER_SYSTEM,
        content=content,
        schema=ASSISTANT_ROUTER_SCHEMA,
        max_tokens=2000,
        effort="low",
    )
    chosen = (payload or {}).get("route")
    chosen = chosen if chosen in {"portal", "education", "health"} else "education"
    if session is not None and response is not None:
        await _record(
            session,
            user_id,
            AssistantSection.EDUCATION,
            question,
            AssistantAnswer(
                answer=f"route:{chosen}",
                section=AssistantSection.EDUCATION,
                trace_id=response.trace_id,
                personalised=False,
            ),
            response,
            feature="assistant_router",
        )
    return chosen


async def ask(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    question: str,
    language: str | None = None,
    route: str = "auto",
    gateway=None,
    history: list | None = None,
) -> AssistantAnswer:
    """One entry point for every question.

    The assistant and the navigator were two products with two chat boxes, and
    a woman with a question had to know in advance which one owned it. She does
    not. The router picks the capability — portal documentation, career advice,
    or age-appropriate health — and the answer comes back in one shape
    whichever route it took.
    """
    gateway = gateway or llm_gateway
    persona = await build_persona(session, user_id, language=_answer_language(question, language))
    lang = persona.language

    # Safety first, before routing and before any model call — a disclosure
    # must not even be classified by a generative system.
    if rag.needs_human_escalation(question):
        answer = await _escalate(session, user_id, AssistantSection.EDUCATION, persona, question)
        answer.route = "safety"
        return answer

    started = time.perf_counter()
    chosen = (
        route
        if route in {"portal", "education", "health"}
        else await classify(question, gateway, session=session, user_id=user_id, history=history)
    )
    logger.info(
        "assistant routed",
        extra={
            "extra_fields": {
                "route": chosen,
                "route_ms": int((time.perf_counter() - started) * 1000),
            }
        },
    )

    if chosen == "portal":
        found = await ai_navigator.answer(
            session,
            question=question,
            user_id=user_id,
            language=lang,
            gateway=gateway,
            history=history,
        )
        answer = AssistantAnswer(
            answer=found.answer,
            section=AssistantSection.EDUCATION,
            trace_id=found.trace_id,
            personalised=persona.personalised,
            route="portal",
            escalated=found.escalated_to_human,
            escalation_reason=found.escalation_reason,
            next_actions=list(found.suggested_actions)[:3],
            confidence=found.confidence,
            is_uncertain=found.is_uncertain,
            resources=list(found.resources),
            degraded=found.escalation_reason in {"model_unavailable", "ai_disabled"},
            sources=[
                {
                    "document_title": source.document_title,
                    "excerpt": source.excerpt,
                    "chunk_id": str(source.chunk_id),
                }
                for source in found.sources[:3]
            ],
        )
        if found.is_uncertain and not found.escalated_to_human:
            # The documentation had no answer; the catalogue may still have
            # a course for what she asked about.
            answer.programs = [_ref(p, lang) for p in await catalogue_matches(session, question)][
                :4
            ]
        return answer

    if chosen == "health":
        answer = await ask_health(
            session,
            user_id=user_id,
            question=question,
            language=lang,
            persona=persona,
            gateway=gateway,
            history=history,
        )
        answer.route = "health"
        return answer

    answer = await ask_education(
        session,
        user_id=user_id,
        question=question,
        language=lang,
        persona=persona,
        gateway=gateway,
        history=history,
    )
    answer.route = "education"
    return answer


# ------------------------------------------------------------------ internals


def _feature(section: AssistantSection) -> str:
    return f"assistant_{section.value}"


async def _complete(
    gateway, *, system, content, schema, max_tokens: int = 16000, effort: str = "medium"
):
    """One structured round-trip.

    `max_tokens` has to cover the thinking tokens as well as the reply: at 4000
    the model spent its budget reasoning and the JSON came back truncated, which
    surfaces as an unparsable payload rather than as an error. Effort is
    `medium` because this is advice writing, not deep reasoning — it roughly
    halves the latency at no visible cost to the answer.

    Bounded by the interactive timeout: past it the caller degrades instead of
    holding the page open.
    """
    try:
        response: LlmResponse = await gateway.complete(
            system=system,
            messages=[{"role": "user", "content": content}],
            json_schema=schema,
            max_tokens=max_tokens,
            effort=effort,
            timeout=settings.ai_interactive_timeout_seconds,
        )
    except LlmUnavailableError as exc:
        logger.warning("assistant unavailable: %s", exc)
        return None, None
    if response.refused or not response.parsed:
        logger.warning("assistant produced no usable payload (refused=%s)", response.refused)
        return None, response
    return response.parsed, response


def _unavailable(
    section: AssistantSection, persona: Persona, response: LlmResponse | None = None
) -> AssistantAnswer:
    return AssistantAnswer(
        answer=_UNAVAILABLE[persona.language if persona.language in _UNAVAILABLE else "uz"],
        section=section,
        trace_id=response.trace_id if response else uuid.uuid4().hex,
        personalised=persona.personalised,
        escalation_reason="model_unavailable",
        degraded=True,
    )


def _degraded_education(
    persona: Persona, matches: list[Program], response: LlmResponse | None
) -> AssistantAnswer:
    """The education answer without a model: the courses that match, said plainly."""
    lang = persona.language if persona.language in _DEGRADED_EDUCATION else "uz"
    if not matches:
        return _unavailable(AssistantSection.EDUCATION, persona, response)
    return AssistantAnswer(
        answer=_DEGRADED_EDUCATION[lang],
        section=AssistantSection.EDUCATION,
        trace_id=response.trace_id if response else uuid.uuid4().hex,
        personalised=persona.personalised,
        programs=[_ref(p, persona.language) for p in matches[:6]],
        escalation_reason="model_unavailable",
        degraded=True,
    )


async def _escalate(
    session: AsyncSession,
    user_id: uuid.UUID | None,
    section: AssistantSection,
    persona: Persona,
    question: str,
) -> AssistantAnswer:
    """The safety reply. No model, no routing, no retrieval.

    A signed-in woman gets a coordinator signal (risk flag, notification,
    audit row) and is told so; a guest is told honestly that nothing was
    passed on. The trace keeps the category, never her words (I-16).
    """
    category = safety.detect(question) or safety.SafetyCategory.PHYSICAL_VIOLENCE
    trace_id = uuid.uuid4().hex
    outcome = await safety.escalate(
        session,
        user_id=user_id,
        category=category,
        surface=_feature(section),
        language=persona.language,
        trace_id=trace_id,
        minor=persona.is_minor and persona.band is not AgeBand.UNKNOWN,
    )
    answer = AssistantAnswer(
        answer=outcome.text,
        section=section,
        trace_id=trace_id,
        personalised=persona.personalised,
        escalated=True,
        escalation_reason="safety_topic_detected",
        resources=outcome.resources,
    )
    await _record(
        session,
        user_id,
        section,
        safety.redacted(category),
        answer,
        None,
        model="guardrail",
        prompt_version="safety-filter",
    )
    return answer


async def _record(
    session: AsyncSession,
    user_id: uuid.UUID | None,
    section: AssistantSection,
    question: str,
    answer: AssistantAnswer,
    response: LlmResponse | None,
    *,
    model: str | None = None,
    prompt_version: str | None = None,
    feature: str | None = None,
    response_text: str | None = None,
) -> None:
    """Persist the trace. Excerpts are scrubbed of direct identifiers."""
    session.add(
        AiInteraction(
            user_id=user_id,
            feature=feature or _feature(section),
            model_version=model or (response.model if response else "unavailable"),
            prompt_version=prompt_version or (response.prompt_version if response else "none"),
            trace_id=answer.trace_id,
            prompt_excerpt=llm_gateway.sanitise(question)[:500],
            response_excerpt=llm_gateway.sanitise(
                response_text if response_text is not None else answer.answer
            )[:2000],
            retrieved_sources=[p["id"] for p in answer.programs],
            confidence=None,
            refused=False,
            escalated=answer.escalated,
            latency_ms=response.latency_ms if response else None,
            input_tokens=response.input_tokens if response else None,
            output_tokens=response.output_tokens if response else None,
        )
    )
    await session.flush()


_OUT_OF_SCOPE = {
    "uz": (
        "Bu mavzu sizning yoshingiz uchun mo'ljallanmagan. Iltimos, ota-onangiz, "
        "ishonchli kattalar yoki shifokor bilan gaplashing — ular sizga to'g'ri "
        "javob beradi. Men esa sog'lom odatlar, uyqu, ovqatlanish va kayfiyat "
        "haqida yordam bera olaman."
    ),
    "ru": (
        "Эта тема не предназначена для твоего возраста. Пожалуйста, поговори с "
        "родителями, взрослым, которому доверяешь, или с врачом — они ответят "
        "правильно. А я помогу с привычками, сном, питанием и настроением."
    ),
    "en": (
        "This topic is not meant for your age. Please talk to a parent, an adult "
        "you trust, or a doctor — they can answer it properly. I can help with "
        "healthy habits, sleep, food and how you are feeling."
    ),
}

_UNAVAILABLE = {
    "uz": "Hozir javob bera olmayapman. Birozdan so'ng yana urinib ko'ring.",
    "ru": "Сейчас не могу ответить. Попробуйте, пожалуйста, чуть позже.",
    "en": "I cannot answer right now. Please try again in a little while.",
}


_DEGRADED_EDUCATION = {
    "uz": (
        "AI yordamçi hozir cheklangan rejimda işlayapti. Savolingizga mos portal kurslari quyida."
    ),
    "ru": (
        "AI-помощник сейчас работает в ограниченном режиме. Ниже — курсы портала, "
        "подходящие под ваш вопрос."
    ),
    "en": (
        "The AI assistant is in limited mode right now. Below are portal courses "
        "that match your question."
    ),
}

#: The level names the diagnostic result page uses (`dr.level.*` in the web
#: catalogue), so the assistant names her level in the same words.
_LEVEL_LABEL = {
    "starting_point": {"uz": "Boşlaniş nuqtasi", "ru": "Точка старта", "en": "Starting point"},
    "building_foundation": {
        "uz": "Poydevor qurilmoqda",
        "ru": "Строим фундамент",
        "en": "Building foundation",
    },
    "good_foundation": {"uz": "Yaxşi poydevor", "ru": "Хороший фундамент", "en": "Good foundation"},
    "strong_area": {"uz": "Kuçli yönaliş", "ru": "Сильное направление", "en": "Strong area"},
    "advanced": {"uz": "Yuqori daraja", "ru": "Продвинутый уровень", "en": "Advanced"},
}


def guest_allowance() -> int:
    return max(0, settings.assistant_guest_questions or GUEST_QUESTION_ALLOWANCE)
