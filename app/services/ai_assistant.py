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
from app.services import ai_navigator, rag
from app.services.age_gate import (
    HEALTH_SCOPE,
    age_from_profile,
    band_for_profile,
    blocks_adult_health,
    is_minor,
)
from app.services.audit_service import has_consent
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
from app.services.scoring import weakest_dimensions

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
    skills: list[str] = field(default_factory=list)
    education_level: str | None = None
    profession: str | None = None
    employment_status: str | None = None
    region: str | None = None
    goals: list[str] = field(default_factory=list)
    directions: list[str] = field(default_factory=list)
    in_progress: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    plan_progress: int | None = None
    personalised: bool = False

    @property
    def is_minor(self) -> bool:
        return is_minor(self.band)

    def as_prompt(self) -> str:
        """The persona block handed to the model. No direct identifiers."""
        if not self.personalised:
            return (
                "READER PROFILE: not available — she is either not signed in or "
                "has not consented to AI personalisation. Answer generally, and "
                "mention once that signing in and completing her profile makes "
                "the advice specific to her."
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
            lines.append(f"- interests: {', '.join(self.interests)}")
        if self.skills:
            lines.append(f"- skills she already has: {', '.join(self.skills)}")
        if self.goals:
            lines.append(f"- her stated goals: {'; '.join(self.goals)}")
        if self.directions:
            lines.append(f"- weakest development dimensions: {', '.join(self.directions)}")
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
        return Persona(band=band, language=language)

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
        skills=list(profile.skills) if profile else [],
        education_level=profile.education_level if profile else None,
        profession=profile.profession if profile else None,
        employment_status=profile.employment_status if profile else None,
        region=user.region.value if user and user.region else None,
        goals=[g.title for g in goals],
        directions=[d.value for d in weakest_dimensions(scores, limit=3)] if scores else [],
        in_progress=[title(p) for e, p in enrolments if e.status is EnrollmentStatus.IN_PROGRESS],
        completed=[title(p) for e, p in enrolments if e.status is EnrollmentStatus.COMPLETED],
        plan_progress=plan.progress_percent if plan else None,
        personalised=True,
    )


async def _catalogue(session: AsyncSession, language: str) -> tuple[str, dict[str, dict]]:
    """The portal's own programmes, so recommendations point at real courses."""
    programs = list(
        (
            await session.execute(select(Program).where(Program.is_published.is_(True)).limit(60))
        ).scalars()
    )
    lines: list[str] = []
    index: dict[str, dict] = {}
    for program in programs:
        title = (
            program.title_i18n.get(language)
            or program.title_i18n.get("uz")
            or next(iter(program.title_i18n.values()), "")
        )
        lines.append(f"- {program.id} | {program.category.value} | {title}")
        index[str(program.id)] = {
            "id": str(program.id),
            "slug": program.slug,
            "title": title,
            "category": program.category.value,
        }
    return "\n".join(lines) or "- (catalogue empty)", index


async def ask_education(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    question: str,
    language: str | None = None,
    persona: Persona | None = None,
    gateway=None,
) -> AssistantAnswer:
    """The conversational half of the education answer.

    Deliberately small: the detailed profession and roadmap cards are a second
    pass. Generation runs at roughly 40 tokens a second, so folding the cards
    into this call pushed it past fifty seconds — long enough that the page
    reads as broken. Split, the reply lands in about ten and the cards arrive
    behind it.
    """
    gateway = gateway or llm_gateway
    persona = persona or await build_persona(session, user_id, language=language)
    lang = language or persona.language

    if rag.needs_human_escalation(question):
        return await _escalate(session, user_id, AssistantSection.EDUCATION, persona, question)

    content = (
        f"{persona.as_prompt()}\n\nANSWER LANGUAGE: {lang}\nQUESTION: {gateway.sanitise(question)}"
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
        return _unavailable(AssistantSection.EDUCATION, persona)

    answer = AssistantAnswer(
        answer=payload.get("answer", ""),
        section=AssistantSection.EDUCATION,
        trace_id=response.trace_id,
        personalised=persona.personalised,
        kind=payload.get("kind", "general"),
        next_actions=list(payload.get("next_actions", []))[:3],
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
) -> AssistantAnswer:
    """The cards: professions or an ordered roadmap, tied to real programmes."""
    gateway = gateway or llm_gateway
    persona = persona or await build_persona(session, user_id, language=language)
    lang = language or persona.language

    catalogue, index = await _catalogue(session, lang)
    content = (
        f"{persona.as_prompt()}\n\n"
        f"PORTAL PROGRAMME CATALOGUE (id | category | title):\n{catalogue}\n\n"
        f"ANSWER LANGUAGE: {lang}\n"
        f"REQUEST KIND: {kind}\n"
        f"QUESTION: {gateway.sanitise(question)}\n"
        f"ANSWER ALREADY GIVEN: {answer_so_far[:1200]}"
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
        return _unavailable(AssistantSection.EDUCATION, persona)

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

    return AssistantAnswer(
        answer="",
        section=AssistantSection.EDUCATION,
        trace_id=response.trace_id,
        personalised=persona.personalised,
        kind=kind,
        professions=list(payload.get("professions", []))[:3],
        roadmap=list(payload.get("roadmap", []))[:7],
        programs=referenced[:6],
    )


async def ask_health(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    question: str,
    language: str | None = None,
    persona: Persona | None = None,
    gateway=None,
) -> AssistantAnswer:
    """Age-appropriate health information. Never a diagnosis."""
    gateway = gateway or llm_gateway
    persona = persona or await build_persona(session, user_id, language=language)
    lang = language or persona.language

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

    system = ASSISTANT_HEALTH_SYSTEM.format(scope=HEALTH_SCOPE[persona.band])
    content = (
        f"{persona.as_prompt()}\n\nANSWER LANGUAGE: {lang}\nQUESTION: {gateway.sanitise(question)}"
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
        return _unavailable(AssistantSection.HEALTH, persona)

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
        return _unavailable(AssistantSection.DAILY, persona)

    answer = AssistantAnswer(
        answer=payload.get("message", ""),
        section=AssistantSection.DAILY,
        trace_id=response.trace_id,
        personalised=persona.personalised,
    )
    await _record(session, user_id, AssistantSection.DAILY, f"daily:{lang}", answer, response)
    return answer


# --------------------------------------------------------------- the one door


async def classify(question: str, gateway=None) -> str:
    """Which capability should answer this. Falls back to education."""
    gateway = gateway or llm_gateway
    if not gateway.enabled:
        return "education"
    payload, _ = await _complete(
        gateway,
        system=ASSISTANT_ROUTER_SYSTEM,
        content=question,
        schema=ASSISTANT_ROUTER_SCHEMA,
        max_tokens=2000,
        effort="low",
    )
    route = (payload or {}).get("route")
    return route if route in {"portal", "education", "health"} else "education"


async def ask(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    question: str,
    language: str | None = None,
    route: str = "auto",
    gateway=None,
) -> AssistantAnswer:
    """One entry point for every question.

    The assistant and the navigator were two products with two chat boxes, and
    a woman with a question had to know in advance which one owned it. She does
    not. A short classification call picks the capability — portal
    documentation, career advice, or age-appropriate health — and the answer
    comes back in one shape whichever route it took.

    Routing costs two to three seconds. That is cheaper than the wrong answer,
    and far cheaper than making her ask twice.
    """
    gateway = gateway or llm_gateway
    persona = await build_persona(session, user_id, language=language)
    lang = language or persona.language

    # Safety first, before routing and before any model call — a disclosure
    # must not even be classified by a generative system.
    if rag.needs_human_escalation(question):
        answer = await _escalate(session, user_id, AssistantSection.EDUCATION, persona, question)
        answer.route = "safety"
        return answer

    chosen = (
        route if route in {"portal", "education", "health"} else await classify(question, gateway)
    )

    if chosen == "portal":
        found = await ai_navigator.answer(
            session, question=question, user_id=user_id, language=lang, gateway=gateway
        )
        return AssistantAnswer(
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
            sources=[
                {
                    "document_title": source.document_title,
                    "excerpt": source.excerpt,
                    "chunk_id": str(source.chunk_id),
                }
                for source in found.sources[:3]
            ],
        )

    if chosen == "health":
        answer = await ask_health(
            session,
            user_id=user_id,
            question=question,
            language=lang,
            persona=persona,
            gateway=gateway,
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
    """
    try:
        response: LlmResponse = await gateway.complete(
            system=system,
            messages=[{"role": "user", "content": content}],
            json_schema=schema,
            max_tokens=max_tokens,
            effort=effort,
        )
    except LlmUnavailableError as exc:
        logger.warning("assistant unavailable: %s", exc)
        return None, None
    if response.refused or not response.parsed:
        logger.warning("assistant produced no usable payload (refused=%s)", response.refused)
        return None, response
    return response.parsed, response


def _unavailable(section: AssistantSection, persona: Persona) -> AssistantAnswer:
    return AssistantAnswer(
        answer=_UNAVAILABLE[persona.language if persona.language in _UNAVAILABLE else "uz"],
        section=section,
        trace_id=uuid.uuid4().hex,
        personalised=persona.personalised,
        escalation_reason="model_unavailable",
    )


async def _escalate(
    session: AsyncSession,
    user_id: uuid.UUID | None,
    section: AssistantSection,
    persona: Persona,
    question: str,
) -> AssistantAnswer:
    answer = AssistantAnswer(
        answer=_ESCALATION[persona.language if persona.language in _ESCALATION else "uz"],
        section=section,
        trace_id=uuid.uuid4().hex,
        personalised=persona.personalised,
        escalated=True,
        escalation_reason="safety_topic_detected",
    )
    await _record(
        session,
        user_id,
        section,
        question,
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
) -> None:
    """Persist the trace. Excerpts are scrubbed of direct identifiers."""
    session.add(
        AiInteraction(
            user_id=user_id,
            feature=_feature(section),
            model_version=model or (response.model if response else None),
            prompt_version=prompt_version or (response.prompt_version if response else None),
            trace_id=answer.trace_id,
            prompt_excerpt=llm_gateway.sanitise(question)[:500],
            response_excerpt=llm_gateway.sanitise(answer.answer)[:2000],
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

_ESCALATION = {
    "uz": (
        "Bu savol bo'yicha sizga tirik mutaxassis yordam berishi kerak. "
        "Murojaatingizni koordinatorga yubordim. Agar xavf ostida bo'lsangiz, "
        "zudlik bilan 1146 ishonch telefoniga murojaat qiling."
    ),
    "ru": (
        "С этим вопросом вам должен помочь живой специалист. Я передала "
        "обращение координатору. Если вы в опасности, срочно позвоните на "
        "телефон доверия 1146."
    ),
    "en": (
        "A real specialist should help you with this. I have passed your message "
        "to a coordinator. If you are in danger, call the 1146 helpline now."
    ),
}

_UNAVAILABLE = {
    "uz": "Hozir javob bera olmayapman. Birozdan so'ng yana urinib ko'ring.",
    "ru": "Сейчас не могу ответить. Попробуйте, пожалуйста, чуть позже.",
    "en": "I cannot answer right now. Please try again in a little while.",
}


def guest_allowance() -> int:
    return max(0, settings.assistant_guest_questions or GUEST_QUESTION_ALLOWANCE)
