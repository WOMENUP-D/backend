"""The AI Coach: where she stands, what to do next, and why.

The architecture is one sentence: **WomanUP data → the deterministic engine →
the model explains.** The model never ranks and never chooses. `services.
recommendation` already decided what she should do next, using her score, her
skills and the live catalogue, and it did so without a model call so that the
answer is the same twice in a row. This module hands that decision to the model
and asks it to explain the decision to her, in her words.

Three things make the grounding real rather than aspirational.

**The context is the only source of facts.** Everything in the prompt comes
from her own records: the Development Score, the canonical skill layer, her
enrollments with the server's own progress, her learning paths, and the
opportunities the engine matched. Nothing is summarised from elsewhere and
nothing is invented to fill a gap.

**The offer list is a closed set.** Every programme, path and listing the model
may name is enumerated with an id. What comes back is resolved against that
index afterwards, so an id the model invented is dropped rather than rendered.
A hallucinated course cannot reach the browser, because the browser is only
ever handed records this module looked up.

**Skills keep their status.** The context says which skills are *learned*,
which are *assessed* and which are *verified*, and the prompt says in as many
words that a course produces the first and never the last. The whole point of
Step 3 is lost if the Coach congratulates her on a verified skill she does not
have.

When the model is unavailable the Coach still answers — from the same context,
composed deterministically. A coaching page that goes blank when the provider
is down is worse than one that says the true thing plainly.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    AssistantSection,
    ConsentScope,
    DimensionBand,
    EnrollmentStatus,
    EventFormat,
    JourneyStage,
    NextStepKind,
    SkillStatus,
    normalise_language,
)
from app.models.audit import AiInteraction
from app.models.practice import PracticalTask
from app.models.profile import Profile
from app.models.program import Certificate, ProgramLesson, ProgramModule
from app.models.user import User
from app.schemas.career_path import CareerPathDetail
from app.schemas.coach import (
    CoachApplication,
    CoachCareer,
    CoachCareerOption,
    CoachContextRead,
    CoachCourse,
    CoachDimension,
    CoachLearning,
    CoachPath,
    CoachPortfolio,
    CoachPractice,
    CoachReference,
    CoachReply,
    CoachScore,
    CoachSkill,
    CoachSkills,
    CoachSuggestion,
)
from app.schemas.event import EventCard
from app.schemas.opportunity import OpportunityDetail
from app.schemas.recommendation import NextStep, OpportunitySuggestion
from app.services import career_path as career_service
from app.services import events as event_service
from app.services import opportunities as opportunity_service
from app.services import portfolio as portfolio_service
from app.services import practice as practice_service
from app.services import rag
from app.services import recommendation as engine
from app.services import skills as skill_service
from app.services.audit_service import has_consent
from app.services.llm_gateway import LlmResponse, LlmUnavailableError, llm_gateway
from app.services.prompts import COACH_SCHEMA, COACH_SYSTEM
from app.services.score_insights import band_for
from app.services.scoring import composite_score

logger = logging.getLogger(__name__)

#: Kept small on purpose. The prompt is an explanation brief, not a database
#: dump, and a model handed forty courses picks one at random.
MAX_OFFER_PROGRAMS = 8
MAX_OFFER_PATHS = 4
MAX_OFFER_OPPORTUNITIES = 4
MAX_OFFER_TASKS = 4
MAX_OFFER_CAREER_ITEMS = 3
MAX_OFFER_EVENTS = 4
MAX_SUGGESTIONS = 4

_OPEN = (EnrollmentStatus.ENROLLED, EnrollmentStatus.IN_PROGRESS)


# ---------------------------------------------------------------------------
# The context
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CoachContext:
    """Her real state, plus the closed set of records the Coach may name."""

    user_id: uuid.UUID
    language: str
    personalised: bool
    read: CoachContextRead
    #: id (as a string) -> the record it stands for. The grounding boundary:
    #: a reference the model returns is kept only if it is a key here.
    offer: dict[str, CoachReference] = field(default_factory=dict)
    #: The prompt lines describing that same offer, so the two cannot diverge.
    offer_lines: list[str] = field(default_factory=list)
    context_lines: list[str] = field(default_factory=list)

    def as_prompt(self) -> str:
        offer = "\n".join(self.offer_lines) or "- (nothing in the catalogue matches her right now)"
        return (
            "CONTEXT — everything WomanUP records about this woman:\n"
            + "\n".join(self.context_lines)
            + "\n\nOFFER — the only records you may name, by id:\n"
            + offer
        )


def _text(values: dict, language: str) -> str:
    return values.get(language) or values.get("uz") or next(iter(values.values()), "")


async def build(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    language: str | None = None,
    opportunity_id: uuid.UUID | None = None,
) -> CoachContext:
    """Assemble what the Coach knows, from her own records and nothing else.

    Reads through the services that already own each fact: the recommendation
    engine for the ranking and the catalogue, the skill layer for what she can
    do, the path layer for the routes she is on, and `services.learning`'s own
    stored progress for how far along each course is. Nothing is recomputed
    here, so the Coach cannot disagree with the screen she came from.
    """
    ctx = await engine.load_context(session, user_id)
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    stored = await session.scalar(select(User.language).where(User.id == user_id))
    # The request's locale wins: it is the language she is reading the site in
    # right now, while the stored column is only as fresh as the last time she
    # changed it.
    language = (
        normalise_language(language) or normalise_language(stored.value if stored else None) or "uz"
    )

    personalised = await has_consent(session, user_id, ConsentScope.AI_PERSONALISATION)

    score = _score_of(ctx)
    skills = await _skills_of(session, ctx)
    learning = await _learning_of(session, ctx, language)
    practice, practice_tasks = await _practice_of(session, ctx, skills, language)
    portfolio = await _portfolio_of(session, ctx.user_id)
    paths = _paths_of(ctx, language)
    careers, career, career_detail = await _career_of(session, ctx)
    applications = await _applications_of(session, user_id)
    focus = await _focus_of(session, user_id, opportunity_id) if opportunity_id else None
    steps = engine.next_steps(ctx)
    opportunities = engine.opportunity_suggestions(ctx)
    events, my_events = await event_service.her_events(session, user_id)

    read = CoachContextRead(
        personalised=personalised,
        name=(profile.full_name.split()[0] if profile and profile.full_name else None),
        language=language,
        score=score,
        skills=skills,
        learning=learning,
        practice=practice,
        portfolio=portfolio,
        paths=paths,
        careers=careers,
        career=career,
        applications=applications,
        next_steps=steps,
        opportunities=opportunities,
        events=events,
        my_events=my_events,
    )
    read.suggestions = _suggestions(read, language)

    offer, offer_lines = _offer(
        ctx,
        steps,
        opportunities,
        practice_tasks,
        language,
        careers=careers,
        career_detail=career_detail,
        focus=focus,
        events=events,
        my_events=my_events,
    )
    context_lines = _context_lines(read, steps, language)
    if focus is not None:
        context_lines += _focus_lines(focus, language)
    return CoachContext(
        user_id=user_id,
        language=language,
        personalised=personalised,
        read=read,
        offer=offer,
        offer_lines=offer_lines,
        context_lines=context_lines,
    )


def _score_of(ctx: engine.LearnerContext) -> CoachScore:
    """Her Development Score, read as bands rather than as bare numbers."""
    if not ctx.scores:
        return CoachScore(assessed=False)

    current = ctx.current
    dimensions = [
        CoachDimension(dimension=dimension, current=round(value, 1), band=band_for(value).value)
        for dimension, value in sorted(current.items(), key=lambda kv: kv[0].value)
    ]
    return CoachScore(
        assessed=True,
        composite=round(composite_score(current), 1),
        dimensions=dimensions,
        strengths=[
            row.dimension
            for row in sorted(dimensions, key=lambda d: -d.current)
            if row.band == DimensionBand.STRONG.value
        ][:3],
        focus=engine.focus_dimensions(ctx),
    )


async def _skills_of(session: AsyncSession, ctx: engine.LearnerContext) -> CoachSkills:
    """What she can do, grouped by what backs it.

    Straight off the canonical layer — `profile.skills` is what she once typed
    about herself, which is one kind of evidence and the weakest of them.
    """
    grouped: dict[SkillStatus | None, list[CoachSkill]] = {}
    for record in await skill_service.user_skills(session, ctx.user_id):
        entry = CoachSkill(
            skill=skill_service.ref_for(record.skill),
            status=record.status,
            level=record.level,
            evidence_count=sum(1 for item in record.evidence if item.revoked_at is None),
        )
        grouped.setdefault(record.status, []).append(entry)

    return CoachSkills(
        verified=grouped.get(SkillStatus.VERIFIED, []),
        assessed=grouped.get(SkillStatus.ASSESSED, []),
        learned=grouped.get(SkillStatus.LEARNED, []),
        self_reported=grouped.get(SkillStatus.SELF_REPORTED, []),
        gaps=engine.skill_gaps(ctx),
    )


async def _learning_of(
    session: AsyncSession, ctx: engine.LearnerContext, language: str
) -> CoachLearning:
    """Her courses, with the progress the learning service stored.

    Never recounted here. `services.learning` owns that number, and a second
    opinion about how far along she is, is how two screens start disagreeing.
    """
    in_progress: list[CoachCourse] = []
    completed: list[CoachCourse] = []

    for enrollment in ctx.enrollments.values():
        program = ctx.programs.get(enrollment.program_id)
        if program is None:
            continue
        course = CoachCourse(
            id=program.id,
            slug=program.slug,
            title_i18n=program.title_i18n,
            progress_percent=enrollment.progress_percent,
            status=enrollment.status.value,
        )
        if enrollment.status == EnrollmentStatus.COMPLETED:
            completed.append(course)
        elif enrollment.status in _OPEN:
            in_progress.append(course)

    in_progress.sort(key=lambda course: -course.progress_percent)

    # Only the course she is furthest along in gets its next lesson looked up:
    # it is the one the Coach will name, and a query per enrollment to fill a
    # field nothing reads is a request she pays for.
    if in_progress:
        lesson = await next_lesson(session, ctx, in_progress[0].id)
        if lesson is not None:
            in_progress[0].next_lesson_slug = lesson.slug
            in_progress[0].next_lesson_title_i18n = lesson.title_i18n

    certificates = (
        await session.scalar(
            select(func.count())
            .select_from(Certificate)
            .where(Certificate.user_id == ctx.user_id, Certificate.revoked_at.is_(None))
        )
    ) or 0

    return CoachLearning(
        in_progress=in_progress,
        completed=completed,
        certificates=int(certificates),
    )


async def next_lesson(
    session: AsyncSession, ctx: engine.LearnerContext, program_id: uuid.UUID
) -> ProgramLesson | None:
    """The first lesson of a course she has not ticked, in contents order."""
    enrollment = ctx.enrollments.get(program_id)
    if enrollment is None:
        return None
    done = {str(item) for item in enrollment.completed_lessons or []}
    rows = await session.execute(
        select(ProgramLesson)
        .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
        .where(ProgramModule.program_id == program_id)
        .order_by(ProgramModule.order_index, ProgramLesson.order_index)
    )
    return next((lesson for lesson in rows.scalars() if str(lesson.id) not in done), None)


async def _practice_of(
    session: AsyncSession,
    ctx: engine.LearnerContext,
    skills: CoachSkills,
    language: str,
) -> tuple[CoachPractice, list[PracticalTask]]:
    """Her practical work: what is open, waiting, needing another go, and done.

    Read through `services.practice`, which owns every one of those states.
    Nothing is counted twice and nothing is inferred — an attempt is where the
    database says it is.
    """
    grouped = await practice_service.state_for(session, ctx.user_id)

    # What needs her: something to fix first, then something to finish. Both
    # are tasks she has already started, so both are real.
    leading = next(
        (a for a in grouped["needs_improvement"]), next((a for a in grouped["started"]), None)
    )
    last_failed = next((a for a in grouped["needs_improvement"] if a.feedback), None)

    # Tasks the catalogue holds for the skills she is missing. A task she has
    # already passed is not offered again; there is nothing left to prove.
    gaps = {gap.skill.slug for gap in skills.gaps if gap.skill.slug}
    available = await practice_service.tasks_for_skills(
        session, gaps, exclude_passed_by=ctx.user_id
    )

    offered = available[:MAX_OFFER_TASKS]
    if leading is not None and all(task.id != leading.task_id for task in offered):
        # The task that needs her leads the offer: it is the one the Coach is
        # most likely to name, and it must be nameable.
        offered = [leading.task, *offered][:MAX_OFFER_TASKS]

    return (
        CoachPractice(
            open_tasks=len(grouped["started"]),
            awaiting_review=len(grouped["submitted"]),
            needs_improvement=len(grouped["needs_improvement"]),
            passed=len(grouped["passed"]),
            next_task_slug=leading.task.slug if leading else None,
            next_task_title_i18n=leading.task.title_i18n if leading else {},
            last_feedback=(last_failed.feedback or "") if last_failed else "",
            available_slugs=[task.slug for task in available[:MAX_OFFER_TASKS]],
        ),
        offered,
    )


async def _portfolio_of(session: AsyncSession, user_id: uuid.UUID) -> CoachPortfolio:
    """What she can show, read through the portfolio service.

    The same derivation the portfolio page uses, so the Coach and the page
    cannot disagree about what she holds. Only titles and counts cross into
    the prompt — never a submission, an evaluator's words or a private URL.
    """
    record = await portfolio_service.portfolio_for(session, user_id)
    return CoachPortfolio(
        certificates=[
            {"title_i18n": item.program_title_i18n, "serial": item.serial_number}
            for item in record.certificates
        ],
        projects=[project.title for project in record.projects],
        achievements=record.overview.achievements,
        is_public=record.settings.is_public,
    )


def _paths_of(ctx: engine.LearnerContext, language: str) -> list[CoachPath]:
    """The routes she is on, with the progress the path layer derives."""
    out: list[CoachPath] = []
    for path in ctx.paths:
        progress = engine.path_progress(ctx, path)
        if progress.status == "not_started" and path.id not in ctx.path_records:
            continue
        upcoming = next(
            (
                item.program
                for item in path.items
                if item.program_id in (progress.next_program_id, progress.current_program_id)
            ),
            None,
        )
        out.append(
            CoachPath(
                id=path.id,
                slug=path.slug,
                title_i18n=path.title_i18n,
                status=progress.status.value,
                percent=progress.percent,
                completed_items=progress.completed_items,
                required_items=progress.required_items,
                next_program_slug=upcoming.slug if upcoming else None,
                next_program_title_i18n=upcoming.title_i18n if upcoming else {},
            )
        )
    out.sort(key=lambda row: -row.percent)
    return out


async def _career_of(
    session: AsyncSession, ctx: engine.LearnerContext
) -> tuple[list[CoachCareerOption], CoachCareer | None, CareerPathDetail | None]:
    """The directions the catalogue offers, and the one she chose, read through
    the career service — so the Coach and the career page cannot disagree about
    which skills she holds for it or which stage she is on."""
    cards = await career_service.catalogue_for(session, ctx.user_id, ctx=ctx)
    options = [
        CoachCareerOption(
            id=card.id,
            slug=card.slug,
            title_i18n=card.title_i18n,
            have=card.fit.have if card.fit else 0,
            total=card.fit.total if card.fit else len(card.skills),
        )
        for card in cards
    ]
    chosen = next((card for card in cards if card.fit and card.fit.chosen), None)
    path = await career_service.by_slug(session, chosen.slug) if chosen else None
    if path is None:
        return options, None, None

    detail = await career_service.detail_for(session, path, ctx.user_id, ctx=ctx)
    journey = detail.journey
    explore = next(stage for stage in detail.stages if stage.stage == JourneyStage.EXPLORE)
    career = CoachCareer(
        id=detail.id,
        slug=detail.slug,
        title_i18n=detail.title_i18n,
        have=journey.have if journey else 0,
        total=journey.total if journey else len(detail.skills),
        missing=[item.skill for item in detail.skill_details if item.status is None],
        current=journey.current.value if journey and journey.current else None,
        next_kind=(
            journey.next_step.kind.value if journey and journey.next_step is not None else None
        ),
        opportunities=explore.total,
        listings_withheld=explore.reason == "adults_only",
        weak_dimensions=[note.dimension for note in journey.dimension_notes] if journey else [],
    )
    return options, career, detail


async def _applications_of(session: AsyncSession, user_id: uuid.UUID) -> list[CoachApplication]:
    """Her applications as the tracker shows them — status words, not notes."""
    return [
        CoachApplication(
            opportunity_id=item.opportunity_id,
            title_i18n=item.opportunity.title_i18n if item.opportunity else {},
            status=item.status.value,
            submitted_at=item.submitted_at.date().isoformat() if item.submitted_at else None,
        )
        for item in await opportunity_service.applications_for(session, user_id)
    ]


async def _focus_of(
    session: AsyncSession, user_id: uuid.UUID, opportunity_id: uuid.UUID
) -> OpportunityDetail | None:
    """The listing she asked from, read exactly as its page reads it. A listing
    that does not exist, or was taken down, is simply not in the context."""
    opportunity = await opportunity_service.visible(session, opportunity_id)
    if opportunity is None:
        return None
    return await opportunity_service.detail(session, opportunity, user_id)


def _reward_text(reward: dict) -> str:
    """The listing's own reward fields, as figures — never rounded, never
    described. Nothing when it states none."""
    parts = []
    if isinstance(reward.get("salary_from"), int | float):
        upper = reward.get("salary_to")
        parts.append(
            f"pay {reward['salary_from']}"
            + (f" to {upper}" if isinstance(upper, int | float) else "")
            + f" {reward.get('currency', 'UZS')}"
        )
    for key, word in (("amount", "amount"), ("stipend", "stipend")):
        if isinstance(reward.get(key), int | float):
            parts.append(f"{word} {reward[key]} {reward.get('currency', 'UZS')}")

    # The terms that come with it. An investment's sum means little without
    # what it costs her, so the share or the rate is never left out.
    def number(key: str) -> int | float | None:
        value = reward.get(key)
        return value if isinstance(value, int | float) and not isinstance(value, bool) else None

    if number("equity_percent") is not None:
        parts.append(f"in return for {number('equity_percent')}% of her business")
    if number("rate_percent") is not None:
        parts.append(f"at {number('rate_percent')}% interest")
    if number("commission_percent") is not None:
        parts.append(f"commission {number('commission_percent')}%")
    if number("sessions") is not None:
        parts.append(f"{number('sessions')} sessions")
    if number("price") is not None:
        price = number("price")
        parts.append(
            "free of charge" if price == 0 else f"price {price} {reward.get('currency', 'UZS')}"
        )
    if reward.get("first_month_free") is True:
        parts.append("first month free")
    if reward.get("repayable") is True:
        parts.append("must be repaid")
    elif reward.get("repayable") is False:
        parts.append("not repaid")
    for key, value in reward.items():
        # A currency code is a unit, not something she receives.
        if key != "currency" and isinstance(value, str) and value.strip():
            parts.append(f'"{value.strip()[:200]}"')
    return "; ".join(parts) or "the listing states nothing — say so, do not estimate"


def _focus_lines(focus: OpportunityDetail, language: str) -> list[str]:
    """The listing she is asking about, and how she stands against it. Facts
    the server computed; the model explains them and adds nothing."""

    def name(ref) -> str:
        return _text(ref.name_i18n, language) or ref.label

    if focus.starts_at is not None:
        return _event_focus_lines(focus, language)
    deadline = focus.deadline.date().isoformat() if focus.deadline else "no deadline stated"
    lines = [
        "- the listing she is asking about (a real record, named in the offer): "
        f"{_text(focus.title_i18n, language)} — {focus.type.value}, "
        f"{focus.organisation or 'organisation not stated'}, "
        f"{focus.region or 'no region stated'}, deadline {deadline}, "
        f"{'open' if focus.is_open else 'closed'}",
        "  - it asks for: " + (", ".join(name(ref) for ref in focus.skills) or "no skills stated"),
        "  - what it offers, exactly as the listing records it: " + _reward_text(focus.reward),
    ]
    if focus.organization is not None:
        lines.append(
            f"  - published on WomanUP by {focus.organization.name}"
            + (" (an organisation WomanUP verified)" if focus.organization.is_verified else "")
        )
    rules = focus.eligibility
    if rules is not None and (rules.age_min is not None or rules.age_max is not None):
        limits = []
        if rules.age_min is not None:
            limits.append(f"from {rules.age_min}")
        if rules.age_max is not None:
            limits.append(f"up to {rules.age_max}")
        lines.append("  - its age rule: " + " ".join(limits))
    fit = focus.fit
    if fit is not None:
        held = [
            f"{name(reason.skill)} ({reason.status.value if reason.status else 'held'}"
            + (f", from {_text(reason.source_i18n, language)}" if reason.source_i18n else "")
            + ")"
            for reason in fit.reasons
            if reason.kind == "skill" and reason.skill is not None
        ]
        lines.append("  - she holds: " + (", ".join(held) or "none of them yet"))
        for gap in fit.missing:
            courses = ", ".join(_text(p["title_i18n"], language) for p in gap.programs)
            lines.append(
                f"  - she is missing: {name(gap.skill)}; WomanUP courses that teach it: "
                + (courses or "none on WomanUP right now")
            )
        if fit.on_career:
            lines.append("  - it is on the career direction she chose")
    verdict = focus.eligibility
    if verdict is not None:
        if verdict.may_apply:
            answer = "the platform finds nothing that stops her applying"
        else:
            answer = f"she cannot apply through WomanUP right now ({verdict.reason})"
        lines.append(
            f"  - can she apply: {answer}. Whether she is chosen is the organisation's decision."
        )
    if focus.application is not None:
        lines.append(f"  - she has already applied; status: {focus.application.status.value}")
    return lines


def _event_focus_lines(focus: OpportunityDetail, language: str) -> list[str]:
    """The event she is asking about. Only what the record states: no agenda,
    speakers, price or dress code unless it says so."""

    def name(ref) -> str:
        return _text(ref.name_i18n, language) or ref.label

    where = (
        "online"
        if focus.format == EventFormat.ONLINE
        else ", ".join(part for part in (focus.venue, focus.region) if part) or "place not stated"
    )
    lines = [
        "- the event she is asking about (a real record, named in the offer): "
        f"{_text(focus.title_i18n, language)} — {focus.type.value}",
        f"  - when: starts {_moment(focus.starts_at)}"
        + (f", ends {_moment(focus.ends_at)}" if focus.ends_at else ""),
        f"  - where: {where}",
        f"  - organiser: {focus.organisation or 'not stated'}",
        "  - it covers: " + (", ".join(name(ref) for ref in focus.skills) or "no topics stated"),
        "  - what it describes itself as: "
        + (_text(focus.description_i18n, language)[:600] or "no description"),
        "  - registration: "
        + ("on the organiser's own website" if focus.external_url else "through WomanUP")
        + (f", closes {_moment(focus.deadline)}" if focus.deadline else ", closes when it starts")
        + ("; open now" if focus.is_open else "; closed"),
        "  - what it offers, exactly as the record states: " + _reward_text(focus.reward),
    ]
    rules = focus.eligibility
    if rules is not None and (rules.age_min is not None or rules.age_max is not None):
        limits = [f"from {rules.age_min}"] if rules.age_min is not None else []
        limits += [f"up to {rules.age_max}"] if rules.age_max is not None else []
        lines.append("  - its age rule: " + " ".join(limits))
    if rules is not None and not rules.may_apply:
        lines.append(f"  - she cannot register through WomanUP ({rules.reason})")
    if focus.application is not None:
        lines.append(f"  - she has registered; status: {focus.application.status.value}")
    return lines


# ---------------------------------------------------------------------------
# What the model is allowed to name
# ---------------------------------------------------------------------------


def _offer(
    ctx: engine.LearnerContext,
    steps: list[NextStep],
    opportunities: list[OpportunitySuggestion],
    tasks: list[PracticalTask],
    language: str,
    *,
    careers: list[CoachCareerOption] | None = None,
    career_detail: CareerPathDetail | None = None,
    focus: OpportunityDetail | None = None,
    events: list[EventCard] | None = None,
    my_events: list[EventCard] | None = None,
) -> tuple[dict[str, CoachReference], list[str]]:
    """The closed set of records the Coach may mention, with its prompt lines.

    Built from the engine's own output plus the catalogue it already filtered
    for her age and region — so a course she may not be offered never enters
    the prompt, and cannot be recommended by a model that never saw it.
    """
    offer: dict[str, CoachReference] = {}
    lines: list[str] = []

    def add(kind: str, record_id: uuid.UUID, slug: str | None, titles: dict, note: str) -> None:
        key = str(record_id)
        if key in offer:
            return
        offer[key] = CoachReference(kind=kind, id=record_id, slug=slug, title_i18n=titles)
        lines.append(f"- {key} | {kind} | {_text(titles, language)} | {note}")

    # The listing she asked from leads, when she may be shown it at all. A
    # listing she cannot apply to because of her age is described in the
    # context but never offered as something to go after.
    if focus is not None and not (
        focus.eligibility is not None and focus.eligibility.reason in ("adults_only", "too_young")
    ):
        add(
            "event" if focus.starts_at else "opportunity",
            focus.id,
            None,
            focus.title_i18n,
            "the event she is asking about"
            if focus.starts_at
            else "the listing she is asking about",
        )
        for gap in focus.fit.missing if focus.fit else []:
            skill = _text(gap.skill.name_i18n, language) or gap.skill.label
            for program in gap.programs:
                add(
                    "program",
                    uuid.UUID(program["id"]),
                    program["slug"],
                    program["title_i18n"],
                    f"course that teaches {skill}",
                )

    # Whatever the ranking already put in front of her leads the list.
    for step in steps:
        if step.program is not None:
            add(
                "program",
                step.program.id,
                step.program.slug,
                step.program.title_i18n,
                f"next step: {step.kind.value}; reason: {step.reason.value}",
            )
        if step.path is not None:
            add(
                "learning_path",
                step.path.id,
                step.path.slug,
                step.path.title_i18n,
                f"next step: {step.kind.value}; {step.path.percent}% done",
            )

    for suggestion in engine.program_suggestions(ctx)[:MAX_OFFER_PROGRAMS]:
        add(
            "program",
            suggestion.id,
            suggestion.slug,
            suggestion.title_i18n,
            "suggested course; teaches "
            + (
                ", ".join(
                    _text(ref.name_i18n, language) or ref.label for ref in suggestion.new_skills[:4]
                )
                or "skills she already holds"
            ),
        )

    for path in engine.path_suggestions(ctx)[:MAX_OFFER_PATHS]:
        add(
            "learning_path",
            path.id,
            path.slug,
            path.title_i18n,
            f"route of {path.program_count} courses, {path.percent}% done",
        )

    for task in tasks:
        add(
            "practical_task",
            task.id,
            task.slug,
            task.title_i18n,
            "practical task; practises "
            + (", ".join(task.skills_practised or []) or "no recorded skill")
            + (f"; about {task.estimated_minutes} minutes" if task.estimated_minutes else ""),
        )

    for item in opportunities[:MAX_OFFER_OPPORTUNITIES]:
        matched = len(item.matched_skills)
        add(
            "opportunity",
            item.id,
            None,
            item.title_i18n,
            f"{item.type.value}; {matched} of her skills match"
            + (f"; missing {len(item.missing_skills)}" if item.missing_skills else ""),
        )

    # Events: the ones ahead she is going to, then the ones worth her time.
    for card in [*(my_events or []), *(events or [])][:MAX_OFFER_EVENTS]:
        add("event", card.id, None, card.title_i18n, "event; " + event_note(card, language))

    # The directions themselves, so the Coach can name one — and, for the one
    # she chose, the courses, tasks and listings the career page shows her.
    chosen_id = career_detail.id if career_detail is not None else None
    for option in careers or []:
        add(
            "career_path",
            option.id,
            option.slug,
            option.title_i18n,
            f"career direction; she holds {option.have} of its {option.total} skills"
            + ("; her chosen direction" if option.id == chosen_id else ""),
        )
    if career_detail is not None:
        for program in career_detail.programs[:MAX_OFFER_CAREER_ITEMS]:
            teaches = ", ".join(
                _text(ref.name_i18n, language) or ref.label for ref in program.new_skills[:4]
            )
            add(
                "program",
                program.id,
                program.slug,
                program.title_i18n,
                "course on her chosen direction" + (f"; teaches {teaches}" if teaches else ""),
            )
        for task in career_detail.tasks[:MAX_OFFER_CAREER_ITEMS]:
            add(
                "practical_task",
                task.id,
                task.slug,
                task.title_i18n,
                "practical task on her chosen direction",
            )
        for item in career_detail.opportunities[:MAX_OFFER_CAREER_ITEMS]:
            add(
                "opportunity",
                item.id,
                None,
                item.title_i18n,
                f"{item.type.value}; open listing on her chosen direction",
            )

    return offer, lines


def _moment(value: datetime | None) -> str:
    """A time as she would read it in Uzbekistan: date and hour, no seconds."""
    if value is None:
        return "not stated"
    return value.astimezone(event_service.TASHKENT).strftime("%Y-%m-%d %H:%M (Tashkent)")


def event_note(card, language: str) -> str:
    """One event as the prompt states it: facts from the record, nothing else."""
    where = (
        "online"
        if card.format == EventFormat.ONLINE
        else ", ".join(part for part in (card.venue, card.region) if part) or "place not stated"
    )
    reasons = ", ".join(_reason_text(reason, language) for reason in card.reasons) or "none"
    return (
        f"{card.type.value}; starts {_moment(card.starts_at)}"
        + (f", ends {_moment(card.ends_at)}" if card.ends_at else "")
        + f"; {where}; organiser {card.organisation or 'not stated'}"
        + f"; why it may suit her: {reasons}"
    )


def _reason_text(reason, language: str) -> str:
    skill = (_text(reason.skill.name_i18n, language) or reason.skill.label) if reason.skill else ""
    return {
        "career": f"covers {skill}, on her chosen direction",
        "learning": f"covers {skill}, taught in {_text(reason.program_title_i18n, language)}, "
        "a course she is taking",
        "skill": f"goes deeper into {skill}, which she has",
        "interest": f"about {skill}, an interest she chose",
        "score": f"covers {skill}; her {reason.dimension} score is {reason.score}/100",
        "own_business": "her profile says she runs a business",
        "region": "held in her region",
    }.get(reason.kind, reason.kind)


def _event_lines(read: CoachContextRead, language: str) -> list[str]:
    lines = []
    if read.my_events:
        lines.append(
            "- events ahead she saved, registered for or set a reminder on: "
            + "; ".join(
                f"{_text(card.title_i18n, language)} — {event_note(card, language)}"
                + (
                    f"; her registration status: {card.application_status.value}"
                    if card.application_status
                    else ""
                )
                for card in read.my_events
            )
        )
    if read.events:
        lines.append(
            "- events worth her time, with the records that say so: "
            + "; ".join(
                f"{_text(card.title_i18n, language)} — {event_note(card, language)}"
                for card in read.events
            )
        )
    if not read.my_events and not read.events:
        lines.append("- events: none that match her records right now")
    return lines


def _context_lines(read: CoachContextRead, steps: list[NextStep], language: str) -> list[str]:
    """The context block. Facts only, each one from a record she owns."""
    lines: list[str] = [f"- answer language: {language}"]

    if read.score.assessed:
        lines.append(f"- Development Score: {read.score.composite}/100")
        lines += [
            f"  - {row.dimension.value}: {row.current}/100 ({row.band})"
            for row in read.score.dimensions
        ]
    else:
        lines.append(
            "- Development Score: she has not taken the diagnostic yet. "
            "Nothing here is personalised to a score."
        )

    def names(entries: list[CoachSkill]) -> str:
        return ", ".join(
            f"{_text(entry.skill.name_i18n, language) or entry.skill.label}"
            + (f" ({entry.level.value})" if entry.level else "")
            for entry in entries
        )

    skills = read.skills
    if any((skills.verified, skills.assessed, skills.learned, skills.self_reported)):
        lines.append("- skills WomanUP has evidence for:")
        if skills.verified:
            lines.append(
                f"  - verified (a person or a placement confirmed): {names(skills.verified)}"
            )
        if skills.assessed:
            lines.append(f"  - assessed (an assessment scored): {names(skills.assessed)}")
        if skills.learned:
            lines.append(f"  - learned (a course taught, NOT verified): {names(skills.learned)}")
        if skills.self_reported:
            lines.append(
                f"  - self-reported (she said so, no evidence): {names(skills.self_reported)}"
            )
    else:
        lines.append(
            "- skills: WomanUP has not recorded any yet. That is a gap in our records, "
            "not a statement that she has no skills — say it that way."
        )

    if skills.gaps:
        lines.append(
            "- skills she does not hold that the live catalogue can teach her: "
            + ", ".join(
                _text(gap.skill.name_i18n, language) or gap.skill.label for gap in skills.gaps
            )
        )

    learning = read.learning
    if learning.in_progress:
        lines.append("- courses she is taking now:")
        for course in learning.in_progress:
            nxt = (
                f"; next lesson: {_text(course.next_lesson_title_i18n, language)}"
                if course.next_lesson_slug
                else ""
            )
            lines.append(
                f"  - {_text(course.title_i18n, language)} — {course.progress_percent}% done{nxt}"
            )
    else:
        lines.append("- courses she is taking now: none")

    if learning.completed:
        lines.append(
            f"- courses finished ({len(learning.completed)}): "
            + ", ".join(_text(c.title_i18n, language) for c in learning.completed[:6])
        )
    lines.append(f"- certificates issued to her: {learning.certificates}")

    practice = read.practice
    if any(
        (practice.open_tasks, practice.awaiting_review, practice.needs_improvement, practice.passed)
    ):
        lines.append(
            "- practical tasks: "
            f"{practice.passed} passed, {practice.needs_improvement} need another go, "
            f"{practice.awaiting_review} waiting to be assessed, {practice.open_tasks} open"
        )
        if practice.next_task_slug:
            lines.append(
                f"  - the one that needs her: {_text(practice.next_task_title_i18n, language)}"
            )
        if practice.last_feedback:
            # The evaluator's own words. Quote them; do not rewrite them, and
            # do not soften a verdict somebody else gave.
            lines.append(
                "  - the evaluator's feedback on her last needs-improvement attempt, "
                f'quoted exactly: "{practice.last_feedback}"'
            )
    else:
        lines.append("- practical tasks: she has not attempted any yet")

    if practice.available_slugs:
        lines.append(
            f"- practical tasks the catalogue holds for skills she is missing: "
            f"{len(practice.available_slugs)} (named in the offer list)"
        )
    else:
        lines.append(
            "- practical tasks matching her skill gaps: none published right now. "
            "Say that plainly rather than describing a task that does not exist."
        )

    portfolio = read.portfolio
    if portfolio.certificates:
        lines.append(
            "- certificates she holds (these and no others exist): "
            + "; ".join(
                f"{_text(item['title_i18n'], language)} (serial {item['serial']})"
                for item in portfolio.certificates
            )
        )
    else:
        lines.append("- certificates she holds: none yet")
    if portfolio.projects:
        # Her own write-ups. Nobody on the platform has checked them, so they
        # are her claims — the context says so, and so must the Coach.
        lines.append(
            "- projects she added to her portfolio herself (her own claims, not verified): "
            + "; ".join(portfolio.projects)
        )
    else:
        lines.append("- projects in her portfolio: none yet")
    lines.append(
        f"- achievements WomanUP can back with a record: {portfolio.achievements}; "
        f"portfolio is {'public' if portfolio.is_public else 'private'}"
    )

    if read.paths:
        lines.append("- learning paths she is on:")
        for path in read.paths:
            upcoming = (
                f"; next course: {_text(path.next_program_title_i18n, language)}"
                if path.next_program_slug
                else ""
            )
            lines.append(
                f"  - {_text(path.title_i18n, language)} — {path.percent}% "
                f"({path.completed_items} of {path.required_items} required){upcoming}"
            )
    else:
        lines.append("- learning paths she is on: none")

    if steps:
        lines.append("- what the platform has already worked out she should do next, in order:")
        for index, step in enumerate(steps, start=1):
            target = ""
            if step.program is not None:
                target = f" -> {_text(step.program.title_i18n, language)}"
            elif step.path is not None:
                target = f" -> {_text(step.path.title_i18n, language)}"
            lines.append(f"  {index}. {step.kind.value} (because: {step.reason.value}){target}")

    if read.opportunities:
        lines.append(
            "- open listings her skills partly match: "
            + ", ".join(_text(item.title_i18n, language) for item in read.opportunities)
        )
    else:
        lines.append("- open listings matching her skills: none right now")

    lines += _career_lines(read, language)
    lines += _event_lines(read, language)

    if read.applications:
        lines.append(
            "- applications she has sent (status exactly as recorded; do not predict outcomes): "
            + "; ".join(
                f"{_text(item.title_i18n, language)} — {item.status}"
                + (f", sent {item.submitted_at}" if item.submitted_at else "")
                for item in read.applications
            )
        )
    else:
        lines.append("- applications she has sent: none")
    return lines


def _career_lines(read: CoachContextRead, language: str) -> list[str]:
    """Career directions: the catalogue, and the one she chose. Facts only."""
    if not read.careers:
        return ["- career directions: WomanUP has none published right now"]

    lines = [
        "- career directions WomanUP offers (these and no others exist): "
        + "; ".join(
            f"{_text(option.title_i18n, language)} (she holds {option.have} of its "
            f"{option.total} skills)"
            for option in read.careers
        )
    ]
    career = read.career
    if career is None:
        lines.append("- her chosen career direction: none yet — choosing one is her decision")
        return lines

    missing = ", ".join(_text(ref.name_i18n, language) or ref.label for ref in career.missing)
    lines.append(
        f"- her chosen career direction: {_text(career.title_i18n, language)}. "
        f"She holds {career.have} of the {career.total} skills it needs"
        + (f"; still missing: {missing}" if missing else "; none missing")
    )
    lines.append(
        "  - stage she is on: "
        + (career.current or "every stage WomanUP can offer is done")
        + (f"; next step: {career.next_kind}" if career.next_kind else "")
    )
    if career.listings_withheld:
        lines.append(
            "  - listings on this direction are not shown to her: she is under 18. "
            "Point her to learning and practice instead."
        )
    else:
        lines.append(f"  - open listings on this direction: {career.opportunities}")
    if career.weak_dimensions:
        lines.append(
            "  - Development Score areas this direction builds that are worth developing: "
            + ", ".join(dimension.value for dimension in career.weak_dimensions)
        )
    return lines


# ---------------------------------------------------------------------------
# Suggested questions
# ---------------------------------------------------------------------------


def _suggestions(read: CoachContextRead, language: str) -> list[CoachSuggestion]:
    """Questions worth asking, chosen from what is actually true of her.

    Keys rather than sentences: the portal is read in four locales, and a
    question composed here would be stuck in the one it was written in. A
    suggestion is only offered when the state it refers to exists — there is no
    point asking "what is next in my path" of a woman who is on none.
    """
    out: list[CoachSuggestion] = []

    if not read.score.assessed:
        out.append(CoachSuggestion(key="coach.q.assessment"))

    if read.learning.in_progress:
        course = read.learning.in_progress[0]
        title = _text(course.title_i18n, language)
        out.append(CoachSuggestion(key="coach.q.course", params={"course": title}))

    # An event she registered for is the most time-bound question she has;
    # one she might go to comes next.
    if read.my_events:
        out.append(
            CoachSuggestion(
                key="coach.q.eventPrep",
                params={"event": _text(read.my_events[0].title_i18n, language)},
            )
        )
    elif read.events:
        out.append(CoachSuggestion(key="coach.q.events"))

    # A route she is still walking, not one she has finished: "what is my next
    # step on X" is a question about an open path, and the finished one sorts
    # first because it is at 100%.
    walking = next((path for path in read.paths if path.status != "completed"), None)
    if walking is not None:
        out.append(
            CoachSuggestion(
                key="coach.q.path", params={"path": _text(walking.title_i18n, language)}
            )
        )

    # Her direction, when she has one: "what do I still need for it" is the
    # question the career page exists to raise.
    if read.career is not None:
        out.append(
            CoachSuggestion(
                key="coach.q.career", params={"career": _text(read.career.title_i18n, language)}
            )
        )

    # Something to fix beats something to start, and both beat a hypothetical.
    if read.practice.needs_improvement and read.practice.next_task_slug:
        out.append(
            CoachSuggestion(
                key="coach.q.taskFailed",
                params={"task": _text(read.practice.next_task_title_i18n, language)},
            )
        )
    elif read.practice.available_slugs:
        out.append(CoachSuggestion(key="coach.q.taskWhy"))

    # Something to show and nothing yet showing it is a real question to ask.
    if (read.portfolio.certificates or read.practice.passed) and not read.portfolio.projects:
        out.append(CoachSuggestion(key="coach.q.portfolio"))

    if read.skills.gaps:
        out.append(CoachSuggestion(key="coach.q.gaps"))

    if read.skills.learned and not read.skills.verified:
        out.append(CoachSuggestion(key="coach.q.verified"))

    if read.opportunities:
        out.append(CoachSuggestion(key="coach.q.jobs"))

    if read.career is None and read.careers:
        out.append(CoachSuggestion(key="coach.q.careerFind"))

    if any(item.status in ("submitted", "in_review") for item in read.applications):
        out.append(CoachSuggestion(key="coach.q.applications"))

    # Always answerable, whatever her state — and the question the Coach exists
    # to answer.
    out.append(CoachSuggestion(key="coach.q.next"))

    seen: set[str] = set()
    unique = [s for s in out if not (s.key in seen or seen.add(s.key))]
    return unique[:MAX_SUGGESTIONS]


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


async def answer(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    question: str,
    language: str | None = None,
    context: CoachContext | None = None,
    gateway=None,
    opportunity_id: uuid.UUID | None = None,
) -> CoachReply:
    """One coaching answer, grounded in her records and in nothing else."""
    gateway = gateway or llm_gateway
    context = context or await build(
        session, user_id, language=language, opportunity_id=opportunity_id
    )

    # Safety before the model, as everywhere else in the AI layer: a disclosure
    # of violence or self-harm goes to a person and is never a prompt.
    if rag.needs_human_escalation(question):
        reply = CoachReply(
            message=_ESCALATION.get(context.language, _ESCALATION["uz"]),
            trace_id=uuid.uuid4().hex,
            personalised=context.personalised,
            escalated=True,
            escalation_reason="safety_topic_detected",
            generated=False,
        )
        await _record(session, user_id, question, reply, None, model="guardrail")
        return reply

    content = (
        f"{context.as_prompt()}\n\n"
        f"HER QUESTION: {gateway.sanitise(question)}\n"
        f"ANSWER LANGUAGE: {context.language}"
    )

    response: LlmResponse | None = None
    payload: dict | None = None
    try:
        response = await gateway.complete(
            system=COACH_SYSTEM,
            messages=[{"role": "user", "content": content}],
            json_schema=COACH_SCHEMA,
            max_tokens=8000,
            effort="low",
        )
        if response.refused or not response.parsed:
            logger.warning("coach produced no usable payload (refused=%s)", response.refused)
        else:
            payload = response.parsed
    except LlmUnavailableError as exc:
        logger.warning("coach unavailable: %s", exc)

    if payload is None:
        # The provider is down. The context is not, so she still gets a true
        # answer rather than an error card — composed from the same records the
        # model would have been given.
        reply = deterministic(context)
        await _record(session, user_id, question, reply, response, model="fallback")
        return reply

    # The grounding boundary. Every id is resolved against the offer index that
    # went into the prompt; anything else the model produced is dropped, so a
    # fabricated course cannot reach the browser.
    references = [
        context.offer[str(value)]
        for value in (payload.get("reference_ids") or [])
        if str(value) in context.offer
    ]
    next_step = context.offer.get(str(payload.get("next_step_id") or ""))
    if next_step is not None and next_step not in references:
        references.insert(0, next_step)

    reply = CoachReply(
        message=(payload.get("message") or "").strip() or deterministic(context).message,
        trace_id=response.trace_id if response else uuid.uuid4().hex,
        personalised=context.personalised,
        unsupported=bool(payload.get("unsupported")),
        next_step=next_step,
        references=references[:6],
    )
    await _record(session, user_id, question, reply, response)
    return reply


def deterministic(context: CoachContext) -> CoachReply:
    """The answer when there is no model: the engine's own, said plainly.

    Not a stub and not an apology. Every sentence is a fact from her records,
    and the step named is the one the deterministic engine chose — which is the
    same step the model would have been asked to explain.
    """
    read = context.read
    language = context.language if context.language in _FALLBACK else "uz"
    copy = _FALLBACK[language]
    parts: list[str] = []

    if read.score.assessed and read.score.composite is not None:
        parts.append(copy["score"].format(score=round(read.score.composite)))
    else:
        parts.append(copy["no_score"])

    if read.learning.in_progress:
        course = read.learning.in_progress[0]
        parts.append(
            copy["course"].format(
                course=_text(course.title_i18n, context.language),
                percent=course.progress_percent,
            )
        )

    step = next((s for s in read.next_steps), None)
    reference: CoachReference | None = None
    if step is not None:
        if step.program is not None:
            reference = context.offer.get(str(step.program.id))
        elif step.path is not None:
            reference = context.offer.get(str(step.path.id))

    if reference is not None:
        key = "next_path" if reference.kind == "learning_path" else "next_program"
        parts.append(copy[key].format(title=_text(reference.title_i18n, context.language)))
    elif step is not None and step.kind == NextStepKind.TAKE_ASSESSMENT:
        parts.append(copy["take_assessment"])
    else:
        parts.append(copy["nothing"])

    parts.append(copy["offline"])

    return CoachReply(
        message=" ".join(parts),
        trace_id=uuid.uuid4().hex,
        personalised=context.personalised,
        generated=False,
        next_step=reference,
        references=[reference] if reference else [],
    )


async def _record(
    session: AsyncSession,
    user_id: uuid.UUID,
    question: str,
    reply: CoachReply,
    response: LlmResponse | None,
    *,
    model: str | None = None,
) -> None:
    """Persist the trace, as section 06 requires of every AI answer."""
    session.add(
        AiInteraction(
            user_id=user_id,
            feature=f"assistant_{AssistantSection.COACH.value}",
            model_version=model or (response.model if response else "unavailable"),
            prompt_version=(response.prompt_version if response else "coach-fallback"),
            trace_id=reply.trace_id,
            prompt_excerpt=llm_gateway.sanitise(question)[:500],
            response_excerpt=llm_gateway.sanitise(reply.message)[:2000],
            # The real records the answer pointed at — the audit trail for
            # "what was she actually shown".
            retrieved_sources=[str(ref.id) for ref in reply.references],
            refused=False,
            escalated=reply.escalated,
            latency_ms=response.latency_ms if response else None,
            input_tokens=response.input_tokens if response else None,
            output_tokens=response.output_tokens if response else None,
        )
    )
    await session.flush()


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

#: The deterministic answer, sentence by sentence. Kept here rather than in the
#: message catalogue because it is composed server-side from her records; the
#: browser receives finished prose, as it does for every other AI answer.
_FALLBACK = {
    "uz": {
        "score": "Hozirgi Development Score: {score}/100.",
        "no_score": "Siz hali diagnostikadan o'tmagansiz, shuning uchun tavsiyalar umumiy.",
        "course": "«{course}» kursi {percent}% bajarilgan.",
        "next_program": "Keyingi qadam: «{title}» kursi.",
        "next_path": "Keyingi qadam: «{title}» yo'nalishi.",
        "take_assessment": "Keyingi qadam: diagnostikadan o'tish.",
        "nothing": "Hozircha katalogda sizga mos yangi taklif yo'q.",
        "offline": "AI yordamchisi vaqtincha ishlamayapti — bu ma'lumot sizning yozuvlaringizdan.",
    },
    "ru": {
        "score": "Текущий Development Score: {score}/100.",
        "no_score": "Вы ещё не прошли диагностику, поэтому рекомендации общие.",
        "course": "Курс «{course}» пройден на {percent}%.",
        "next_program": "Следующий шаг: курс «{title}».",
        "next_path": "Следующий шаг: направление «{title}».",
        "take_assessment": "Следующий шаг: пройти диагностику.",
        "nothing": "Сейчас в каталоге нет подходящего для вас нового предложения.",
        "offline": "AI-помощник временно недоступен — это данные из ваших записей.",
    },
    "en": {
        "score": "Your Development Score is {score}/100.",
        "no_score": (
            "You have not taken the diagnostic yet, so this is general rather than personal."
        ),
        "course": "You are {percent}% through “{course}”.",
        "next_program": "Your next step is the course “{title}”.",
        "next_path": "Your next step is the learning path “{title}”.",
        "take_assessment": "Your next step is to take the diagnostic.",
        "nothing": "There is nothing in the catalogue that matches you right now.",
        "offline": "The AI assistant is unavailable — this comes straight from your own records.",
    },
}

__all__ = [
    "CoachContext",
    "answer",
    "build",
    "deterministic",
]
