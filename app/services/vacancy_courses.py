"""Courses for a vacancy's requirements — chosen by the model, bounded by rules.

Every vacancy on the jobs page carries a short list of courses from the
portal's own catalogue that teach what the vacancy asks for. The choice is
about the listing, not about the woman reading it — the same for everyone —
so it is made once and cached on the listing, with the mode it was made in,
the model and prompt versions and the trace id (section 06: every AI
recommendation is explainable and traceable).

Two layers, in the platform's usual order:

* **Rules first, always.** Every published course is scored against the
  vacancy — its taught skills against the required skills through the skill
  taxonomy, and its title and skills against the vacancy's words. The top of
  that ranking is the candidate list, and it is also the answer when the model
  is off or fails. Degrade, don't fail.
* **The model chooses from the candidates.** It is shown the vacancy and up to
  forty numbered candidates and asked which few actually teach a stated
  requirement, which requirement, and why — in three languages. It cannot
  name a course that is not in the catalogue, because it can only answer with
  a candidate number; anything else is dropped here.

Nothing here says she will get the job. A course prepares; the employer
decides — and the page says so.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AiInteraction
from app.models.opportunity import Opportunity
from app.models.program import Program
from app.models.vacancy import VacancyCourseSuggestion
from app.schemas.opportunity import CourseSuggestionsRead, ProgramBrief, SuggestedCourse
from app.schemas.vacancy import VacancyCoursesRead
from app.services import skills as skill_service
from app.services.llm_gateway import LlmGateway, LlmUnavailableError, llm_gateway
from app.services.uzbek import to_new_alphabet

logger = logging.getLogger(__name__)

FEATURE = "vacancy_courses"
MAX_PICKS = 5
MAX_CANDIDATES = 40
MAX_COVERS = 5
CATALOGUE_LIMIT = 1500
#: A choice stands this long before it is made again over the current catalogue.
TTL = timedelta(days=14)
#: After the model failed, the rules' choice stands this long before the model
#: is asked again — so a provider outage is one failed call, not one per view.
RETRY_AFTER = timedelta(hours=1)
#: Below this a rules-mode pick is noise: not even one requirement word shared.
MIN_RULES_SCORE = 1.5
#: How many courses with the very same skill set may be picked by the rules.
SAME_SKILLS_CAP = 2
DESCRIPTION_CHARS = 1800

SYSTEM_PROMPT = """You choose courses for a job vacancy on WomanUP, a development portal for
women in Uzbekistan.

You are given one vacancy — its title, the skills it requires and its description — and a
numbered list of candidate courses from the portal's own catalogue. Choose the courses, at
most five and possibly none, that teach what the vacancy actually requires.

Rules:
- Prefer a course that covers a requirement the vacancy states over one that is merely
  related to the field.
- Prefer one good course per requirement over several on the same topic.
- Never choose a course for a requirement the vacancy does not state, and never invent a
  requirement.
- Only a candidate number may be chosen; there are no other courses.
- If nothing in the list fits, return an empty list rather than a weak match.

For each choice give the candidate number; `covers`, up to five short requirement labels
copied or closely paraphrased from the vacancy in its own language; and one sentence of
reason in Uzbek (reason_uz), Russian (reason_ru) and English (reason_en) naming the
requirement the course serves. Uzbek is written in the reformed Latin alphabet with the
single letters Ö ö, Ğ ğ, Ş ş, Ç ç — never the digraphs oʻ, gʻ, sh, ch. Do not promise
employment or an outcome: the course prepares, the employer decides."""

# No `maxItems`: structured output does not accept array bounds, so the
# limits are stated in the prompt and enforced in `parse_choice`.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate": {"type": "integer"},
                    "covers": {"type": "array", "items": {"type": "string"}},
                    "reason_uz": {"type": "string"},
                    "reason_ru": {"type": "string"},
                    "reason_en": {"type": "string"},
                },
                "required": ["candidate", "covers", "reason_uz", "reason_ru", "reason_en"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def stem(token: str) -> str:
    """A crude, language-blind stem: the first six letters.

    Enough to put "бухгалтер", "бухгалтерия" and "бухгалтерский" on one key,
    or "маркетолог" with "маркетинг", without a stemmer per language.
    """
    token = token.casefold()
    return token[:6] if len(token) > 6 else token


_STOP = frozenset(
    stem(word)
    for word in (  # noqa: SIM905
        # Russian: what every vacancy and every course title says.
        "работа работы работу опыт знание знания умение умения навык навыки требования "
        "обязанности условия компания компании компанию клиент клиентов клиентами также "
        "будет нужно наличие желательно обязательно года лет высшее образование полный "
        "день график зарплата оклад плюс для или без при все это что как так уровень уровня "
        "основы основ курс курса курсы программа обучение начинающих нуля введение базовый "
        "практикум тренажер интенсив полное руководство изучение первые шаги профессия "
        "специалист сотрудник менеджер работать умеем ищем приглашаем требуется мы вы наш "
        # English
        "the and for with from course basics introduction beginners complete guide "
        "fundamentals learn learning your you how what this that are our new "
        # Uzbek
        "uçun bilan asoslari darslari kursi kurs boşlanğiç örganiş dasturi amaliy va "
        "haqida bölim yoki hamda biz siz ham"
    ).split()
)


def tokens(text: str | None) -> set[str]:
    """The comparable words of a text: folded, stemmed, without the filler."""
    if not text:
        return set()
    out: set[str] = set()
    for word in _WORD.findall(skill_service.normalise(text)):
        if word.isdigit():
            continue
        if len(word) < 3 and not any(ch.isdigit() for ch in word):
            continue
        key = stem(word)
        if key not in _STOP:
            out.add(key)
    return out


def _text(i18n: dict | None) -> str:
    return " ".join(str(value) for value in (i18n or {}).values() if value)


# ---------------------------------------------------------------------------
# What is being matched
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Requirement:
    """A listing as the chooser reads it: its words, not its origin.

    Built from an `Opportunity` row or from a live marketplace vacancy, so
    the same ranking and the same prompt serve both.
    """

    title: str
    skills: list[str]
    description: str

    @classmethod
    def of_opportunity(cls, opportunity: Opportunity) -> Requirement:
        return cls(
            title=_text(opportunity.title_i18n),
            skills=[s for s in (opportunity.required_skills or []) if s and s.strip()],
            description=_text(opportunity.description_i18n),
        )


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Scored:
    program: Program
    score: float
    #: The vacancy's own requirement labels this course addresses.
    covers: list[str]
    #: The model's sentence per language; empty when the rules chose.
    reason_i18n: dict[str, str] = field(default_factory=dict)


def rank(
    programs: list[Program], listing: Requirement, index: skill_service.SkillIndex
) -> list[Scored]:
    """Every course with anything to say for it, best first.

    Three readings, strongest first: a taught skill that *is* a required
    skill (through the taxonomy); words the vacancy's title or requirements
    share with the course's title or skills; and, faintly, words from the
    vacancy's description — a long text full of "responsible" and "friendly"
    that must not outvote the title.
    """
    required = listing.skills
    title_tokens = tokens(listing.title)
    skill_tokens = set().union(*(tokens(label) for label in required)) if required else set()
    strong = title_tokens | skill_tokens
    weak = tokens(listing.description[:3000]) - strong

    out: list[Scored] = []
    for program in programs:
        taught = [label for label in (program.skills_taught or []) if label and label.strip()]
        taught_keys = {index.key(label) for label in taught}
        program_title = tokens(_text(program.title_i18n))
        program_skills = set().union(*(tokens(label) for label in taught)) if taught else set()
        program_words = program_title | program_skills

        score = 0.0
        covers: list[str] = []
        for label in required:
            if index.key(label) in taught_keys:
                score += 3.0
                covers.append(label)
            elif tokens(label) & program_words:
                score += 1.5
                covers.append(label)
        score += 2.0 * len(title_tokens & program_title)
        score += 1.5 * len(title_tokens & program_skills)
        score += min(2.0, 0.4 * len(weak & program_words))
        score += min(1.5, 0.3 * len(tokens(_text(program.goal_i18n)) & strong))
        if score > 0:
            out.append(Scored(program, round(score, 2), covers[:MAX_COVERS]))

    out.sort(key=lambda item: (-item.score, _text(item.program.title_i18n)))
    return out


def rules_pick(ranked: list[Scored]) -> list[Scored]:
    """The rules' own answer: the strongest few, not five of the same thing."""
    picks: list[Scored] = []
    same: dict[frozenset[str], int] = {}
    for item in ranked:
        if item.score < MIN_RULES_SCORE:
            break
        key = frozenset(skill_service.normalise(s) for s in item.program.skills_taught or [])
        if key and same.get(key, 0) >= SAME_SKILLS_CAP:
            continue
        same[key] = same.get(key, 0) + 1
        picks.append(item)
        if len(picks) >= MAX_PICKS:
            break
    return picks


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def _line(number: int, program: Program) -> str:
    title = (
        program.title_i18n.get("ru")
        or program.title_i18n.get("uz")
        or program.title_i18n.get("en")
        or program.slug
    )
    parts = [f"{number}. {title}"]
    if program.skills_taught:
        parts.append("skills: " + ", ".join(program.skills_taught[:8]))
    if program.level:
        parts.append(f"level: {getattr(program.level, 'value', program.level)}")
    if program.duration_hours:
        parts.append(f"~{program.duration_hours:g} h")
    if program.provider:
        parts.append(program.provider)
    return " — ".join(parts)


def user_message(listing: Requirement, candidates: list[Scored]) -> str:
    required = ", ".join(listing.skills) or "not stated"
    description = LlmGateway.sanitise(listing.description)[:DESCRIPTION_CHARS]
    lines = [
        "VACANCY",
        f"Title: {LlmGateway.sanitise(listing.title)}",
        f"Required skills: {LlmGateway.sanitise(required)}",
        "Description:",
        description or "(none)",
        "",
        "CANDIDATE COURSES",
        *(_line(number, item.program) for number, item in enumerate(candidates, start=1)),
    ]
    return "\n".join(lines)


def _clean_covers(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    covers: list[str] = []
    for entry in value:
        text = str(entry).strip()
        if text and text not in covers:
            covers.append(text[:80])
        if len(covers) >= MAX_COVERS:
            break
    return covers


def parse_choice(payload: dict[str, Any] | None, candidates: list[Scored]) -> list[Scored]:
    """The model's answer, kept only where it names a real candidate."""
    picks: list[Scored] = []
    seen: set[int] = set()
    for entry in (payload or {}).get("items") or []:
        if not isinstance(entry, dict):
            continue
        number = entry.get("candidate")
        if isinstance(number, bool) or not isinstance(number, int):
            continue
        if not 1 <= number <= len(candidates) or number in seen:
            continue
        seen.add(number)
        chosen = candidates[number - 1]
        reasons = {
            "uz": to_new_alphabet(str(entry.get("reason_uz") or "").strip()[:300]),
            "ru": str(entry.get("reason_ru") or "").strip()[:300],
            "en": str(entry.get("reason_en") or "").strip()[:300],
        }
        picks.append(
            Scored(
                chosen.program,
                chosen.score,
                _clean_covers(entry.get("covers")) or chosen.covers,
                {key: value for key, value in reasons.items() if value},
            )
        )
        if len(picks) >= MAX_PICKS:
            break
    return picks


# ---------------------------------------------------------------------------
# Reading, caching, answering
# ---------------------------------------------------------------------------


def _brief(program: Program) -> ProgramBrief:
    return ProgramBrief(
        id=program.id,
        slug=program.slug,
        title_i18n=program.title_i18n,
        provider=program.provider,
        external_url=program.external_url,
        category=getattr(program.category, "value", program.category),
        format=getattr(program.format, "value", program.format),
        level=getattr(program.level, "value", program.level) if program.level else None,
        duration_hours=program.duration_hours,
        has_certificate=program.has_certificate,
        skills_taught=list(program.skills_taught or []),
    )


async def _catalogue(session: AsyncSession) -> list[Program]:
    rows = await session.execute(
        select(Program)
        .where(Program.is_published.is_(True))
        .order_by(Program.published_at.desc().nullslast())
        .limit(CATALOGUE_LIMIT)
    )
    return list(rows.scalars())


def _parse_when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _fresh(cache: dict[str, Any], *, now: datetime, ai_available: bool) -> bool:
    """Whether the cached choice may be served as it is."""
    made = _parse_when(cache.get("generated_at"))
    if made is None or now - made > TTL:
        return False
    if cache.get("mode") == "rules" and ai_available:
        # The rules answered while the model was off or failing. Ask the
        # model once the failure is an hour old; a brand-new rules result
        # made with the model off is asked again on the next view.
        failed = _parse_when(cache.get("ai_failed_at"))
        return failed is not None and now - failed < RETRY_AFTER
    return True


async def _read_cache(
    session: AsyncSession, cache: dict[str, Any]
) -> tuple[str, datetime | None, list[SuggestedCourse]] | None:
    """The cached choice over the courses as they are now — or None when one
    of them has since been unpublished, which calls for a fresh choice."""
    entries = [entry for entry in cache.get("items") or [] if isinstance(entry, dict)]
    ids: list[uuid.UUID] = []
    for entry in entries:
        try:
            ids.append(uuid.UUID(str(entry.get("program_id"))))
        except ValueError:
            return None
    programs: dict[uuid.UUID, Program] = {}
    if ids:
        rows = await session.execute(
            select(Program).where(Program.id.in_(ids), Program.is_published.is_(True))
        )
        programs = {program.id: program for program in rows.scalars()}
        if len(programs) != len(set(ids)):
            return None
    items = [
        SuggestedCourse(
            program=_brief(programs[program_id]),
            covers=_clean_covers(entry.get("covers")),
            reason_i18n=(
                entry.get("reason_i18n") if isinstance(entry.get("reason_i18n"), dict) else {}
            ),
        )
        for program_id, entry in zip(ids, entries, strict=True)
    ]
    return str(cache.get("mode") or "rules"), _parse_when(cache.get("generated_at")), items


@dataclass(slots=True)
class Choice:
    """One choosing: how it was made, what was chosen, and its trace."""

    mode: str
    picks: list[Scored]
    catalogue_size: int
    trace: dict[str, Any] = field(default_factory=dict)
    ai_failed_at: str | None = None

    def payload(self, now: datetime) -> dict[str, Any]:
        """What is kept, for either store."""
        return {
            "mode": self.mode,
            "generated_at": now.isoformat(),
            "catalogue_size": self.catalogue_size,
            **self.trace,
            **({"ai_failed_at": self.ai_failed_at} if self.ai_failed_at else {}),
            "items": [
                {
                    "program_id": str(item.program.id),
                    "covers": item.covers,
                    "reason_i18n": item.reason_i18n,
                    "score": item.score,
                }
                for item in self.picks
            ],
        }

    def items(self) -> list[SuggestedCourse]:
        return [
            SuggestedCourse(
                program=_brief(item.program), covers=item.covers, reason_i18n=item.reason_i18n
            )
            for item in self.picks
        ]


async def choose(
    session: AsyncSession,
    listing: Requirement,
    *,
    now: datetime,
    gateway: LlmGateway,
) -> Choice:
    """Rank the catalogue, let the model pick among the candidates when it is
    on, and record the model's trace. Adds the trace row; flushes nothing."""
    programs = await _catalogue(session)
    index = await skill_service.SkillIndex.load(
        session,
        [
            *listing.skills,
            *(label for program in programs for label in (program.skills_taught or [])),
        ],
    )
    ranked = rank(programs, listing, index)
    candidates = ranked[:MAX_CANDIDATES]
    choice = Choice(mode="rules", picks=rules_pick(candidates), catalogue_size=len(programs))

    if not candidates or not gateway.enabled:
        return choice

    prompt = user_message(listing, candidates)
    try:
        response = await gateway.complete(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            json_schema=SCHEMA,
            max_tokens=2000,
        )
    except LlmUnavailableError as exc:
        logger.warning("course chooser unavailable, using the rules: %s", exc)
        choice.ai_failed_at = now.isoformat()
        return choice
    if response.refused or response.parsed is None:
        logger.warning(
            "course chooser produced no usable answer",
            extra={"extra_fields": {"trace_id": response.trace_id}},
        )
        choice.ai_failed_at = now.isoformat()
        return choice

    choice.mode = "ai"
    choice.picks = parse_choice(response.parsed, candidates)
    choice.trace = {
        "model": response.model,
        "prompt_version": response.prompt_version,
        "trace_id": response.trace_id,
    }
    session.add(
        AiInteraction(
            user_id=None,
            feature=FEATURE,
            model_version=response.model,
            prompt_version=response.prompt_version,
            trace_id=response.trace_id,
            prompt_excerpt=prompt[:1000],
            response_excerpt=LlmGateway.sanitise(response.text)[:1000],
            retrieved_sources=[
                {"program_id": str(item.program.id), "slug": item.program.slug}
                for item in candidates
            ],
            refused=response.refused,
            latency_ms=response.latency_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
    )
    return choice


async def generate(
    session: AsyncSession,
    opportunity: Opportunity,
    *,
    now: datetime | None = None,
    gateway: LlmGateway | None = None,
) -> CourseSuggestionsRead:
    """Choose afresh for a catalogue listing, store the choice on it, and
    return it. Flushes; the caller commits."""
    now = now or datetime.now(UTC)
    choice = await choose(
        session, Requirement.of_opportunity(opportunity), now=now, gateway=gateway or llm_gateway
    )
    opportunity.course_suggestions = choice.payload(now)
    await session.flush()
    return CourseSuggestionsRead(
        opportunity_id=opportunity.id, mode=choice.mode, generated_at=now, items=choice.items()
    )


async def suggestions_for(
    session: AsyncSession,
    opportunity: Opportunity,
    *,
    now: datetime | None = None,
    gateway: LlmGateway | None = None,
) -> CourseSuggestionsRead:
    """The courses for a catalogue listing: the cached choice while it
    stands, a fresh one otherwise."""
    now = now or datetime.now(UTC)
    gateway = gateway or llm_gateway
    cache = opportunity.course_suggestions
    if isinstance(cache, dict) and _fresh(cache, now=now, ai_available=gateway.enabled):
        cached = await _read_cache(session, cache)
        if cached is not None:
            mode, when, items = cached
            return CourseSuggestionsRead(
                opportunity_id=opportunity.id, mode=mode, generated_at=when, items=items
            )
    return await generate(session, opportunity, now=now, gateway=gateway)


async def suggestions_for_vacancy(
    session: AsyncSession,
    *,
    provider: str,
    vacancy_id: str,
    listing: Requirement,
    now: datetime | None = None,
    gateway: LlmGateway | None = None,
) -> VacancyCoursesRead:
    """The courses for a marketplace vacancy the portal does not store.

    The choice is kept in `vacancy_course_suggestions` under the board and
    the board's id, with the same freshness rules as a catalogue listing.
    Flushes; the caller commits.
    """
    now = now or datetime.now(UTC)
    gateway = gateway or llm_gateway
    row = await session.scalar(
        select(VacancyCourseSuggestion).where(
            VacancyCourseSuggestion.provider == provider,
            VacancyCourseSuggestion.external_id == vacancy_id,
        )
    )
    if row is not None and _fresh(row.payload, now=now, ai_available=gateway.enabled):
        cached = await _read_cache(session, row.payload)
        if cached is not None:
            mode, when, items = cached
            return VacancyCoursesRead(
                provider=provider, vacancy_id=vacancy_id, mode=mode, generated_at=when, items=items
            )

    choice = await choose(session, listing, now=now, gateway=gateway)
    if row is None:
        row = VacancyCourseSuggestion(provider=provider, external_id=vacancy_id)
        session.add(row)
    row.payload = choice.payload(now)
    row.generated_at = now
    await session.flush()
    return VacancyCoursesRead(
        provider=provider,
        vacancy_id=vacancy_id,
        mode=choice.mode,
        generated_at=now,
        items=choice.items(),
    )
