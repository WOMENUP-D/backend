"""Career paths: a direction toward a kind of work, read against her own records.

A career path stores almost nothing — a name, a promise, a list of skills, a
learning path and the kinds of listing it leads to. Everything else is *read*:

* **Related courses, tasks and listings** are the live catalogue's, matched on
  the path's skills through the skill index. A course that teaches one of them
  belongs to the path; so does a task that practises one and an open listing of
  the right kind that asks for one. Nothing is named that the database does not
  hold, and a listing that has closed simply stops appearing.
* **Where she stands** comes from the records the rest of the platform already
  owns: her skills and their status, her enrollments, her task attempts, her
  portfolio and her applications. Nothing is ticked, and nothing is stored.

It is not a second recommendation engine. It runs on the engine's own
`LearnerContext` — the same catalogue filtered for her age and region, the same
held skills, the same progress readings — and names what it finds with the
engine's own suggestion builders and next-step kinds. The career page and the
cabinet therefore describe the same course, task or listing the same way.

The journey is read in four stages after "you are here":

1. **Learn** — done when every skill the path needs that WomanUP teaches is at
   least *learned*, or when the path's learning path is completed. A skill she
   only listed herself is not learned: that needs evidence.
2. **Practice** — done when she has passed a practical task for this work.
3. **Build** — done when she has described work of her own for it in her
   portfolio. Certificates and passed tasks already appear there by themselves.
4. **Explore** — done once she has applied to a listing on this direction.

A stage the catalogue holds nothing for is *unavailable*, with a reason, and is
skipped rather than blocking the ones after it. Applying is always her decision:
nothing here applies, enrols or starts anything on her behalf.

Listings are withheld from a woman the platform knows to be under 18. A vacancy
or an investment is not a next step to put in front of a child; learning and
practice are, and the page says so.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ApplicationStatus,
    CareerCategory,
    DimensionBand,
    EnrollmentStatus,
    JourneyStage,
    NextStepKind,
    RecommendationReason,
    ScoreDimension,
    SkillStatus,
    StageStatus,
    TaskStatus,
)
from app.models.career_path import CareerPath, UserCareerPath
from app.models.learning_path import LearningPath
from app.models.opportunity import Application, Opportunity
from app.models.practice import PracticalTask
from app.models.program import Program
from app.schemas.career_path import (
    CareerCounts,
    CareerEvidence,
    CareerFit,
    CareerJourney,
    CareerPathDetail,
    CareerPathRead,
    CareerSkill,
    DimensionNote,
    StageRead,
)
from app.schemas.learning_path import PathStatus
from app.schemas.recommendation import NextStep, OpportunitySuggestion
from app.schemas.skill import SkillRef
from app.services import learning_path as path_service
from app.services import portfolio as portfolio_service
from app.services import practice as practice_service
from app.services import recommendation as engine
from app.services import skills as skill_service
from app.services.score_insights import band_for

#: Enough to choose from, few enough to read on a phone.
MAX_PROGRAMS = 6
MAX_TASKS = 6
MAX_OPPORTUNITIES = 6

#: Statuses that mean a course or an assessment taught or scored the skill.
#: What she listed herself is a claim, and a claim does not finish "Learn".
EVIDENCED = (SkillStatus.LEARNED, SkillStatus.ASSESSED, SkillStatus.VERIFIED)

_OPEN_ENROLLMENT = (EnrollmentStatus.ENROLLED, EnrollmentStatus.IN_PROGRESS)
#: A draft was never sent and a withdrawn one was taken back.
_NOT_SENT = (ApplicationStatus.DRAFT, ApplicationStatus.WITHDRAWN)
_NO_DEADLINE = datetime.max.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


async def catalogue(
    session: AsyncSession, *, category: CareerCategory | None = None
) -> list[CareerPath]:
    """Published directions, in curation order."""
    stmt = select(CareerPath).where(CareerPath.is_published.is_(True))
    if category is not None:
        stmt = stmt.where(CareerPath.category == category)
    return list((await session.execute(stmt.order_by(CareerPath.order_index))).scalars())


async def by_slug(session: AsyncSession, slug: str) -> CareerPath | None:
    return await session.scalar(
        select(CareerPath).where(CareerPath.slug == slug, CareerPath.is_published.is_(True))
    )


async def choice_of(session: AsyncSession, user_id: uuid.UUID) -> UserCareerPath | None:
    """Her chosen direction, if it is still published."""
    record = await session.scalar(select(UserCareerPath).where(UserCareerPath.user_id == user_id))
    if record is None:
        return None
    path = await session.get(CareerPath, record.career_path_id)
    return record if path is not None and path.is_published else None


async def choose(session: AsyncSession, *, user_id: uuid.UUID, path: CareerPath) -> UserCareerPath:
    """Make this her direction. Idempotent; choosing another moves the one row.

    Starts nothing and enrols her in nothing: the direction is a choice about
    what to show her first, not a commitment the platform acts on for her.
    """
    record = await session.scalar(select(UserCareerPath).where(UserCareerPath.user_id == user_id))
    now = datetime.now(UTC)
    if record is None:
        record = UserCareerPath(user_id=user_id, career_path_id=path.id, chosen_at=now)
        session.add(record)
    elif record.career_path_id != path.id:
        record.career_path_id = path.id
        record.chosen_at = now
    await session.flush()
    return record


async def clear(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    """Forget her direction. Her courses, tasks and portfolio are untouched."""
    record = await session.scalar(select(UserCareerPath).where(UserCareerPath.user_id == user_id))
    if record is not None:
        await session.delete(record)
        await session.flush()


# ---------------------------------------------------------------------------
# The context: the engine's, for her; the bare catalogue, for a visitor
# ---------------------------------------------------------------------------


async def _anonymous_context(session: AsyncSession) -> engine.LearnerContext:
    """The live catalogue with nobody in it, for a visitor.

    The same four readings the engine loads — published courses, open listings,
    learning paths, practical tasks — and none of the personal ones. Unfiltered
    by age: the programme and opportunity pages show a visitor all of it too.
    """
    now = datetime.now(UTC)
    published = list(
        (
            await session.execute(
                select(Program)
                .where(Program.is_published.is_(True))
                .order_by(Program.published_at.desc().nullslast())
                .limit(engine.CATALOGUE_LIMIT)
            )
        ).scalars()
    )
    opportunities = list(
        (
            await session.execute(
                select(Opportunity)
                .where(
                    Opportunity.is_active.is_(True),
                    or_(Opportunity.deadline.is_(None), Opportunity.deadline > now),
                    Opportunity.starts_at.is_(None),
                )
                .order_by(Opportunity.deadline.asc().nullslast())
                .limit(engine.CATALOGUE_LIMIT)
            )
        ).scalars()
    )
    paths = await path_service.catalogue(session)
    tasks = await practice_service.catalogue(session)

    labels = {label for program in published for label in program.skills_taught}
    labels.update(label for item in opportunities for label in item.required_skills)
    labels.update(label for path in paths for label in path_service.path_labels(path))
    labels.update(label for task in tasks for label in (task.skills_practised or []))

    return engine.LearnerContext(
        user_id=uuid.UUID(int=0),
        age=None,
        region=None,
        skills=[],
        index=await skill_service.SkillIndex.load(session, labels),
        held=set(),
        scores={},
        enrollments={},
        programs={program.id: program for program in published},
        catalogue=published,
        opportunities=opportunities,
        active_plan=None,
        has_draft_plan=False,
        paths=paths,
        path_records={},
        tasks=tasks,
        task_attempts={},
    )


async def _include(
    session: AsyncSession, ctx: engine.LearnerContext, paths: Iterable[CareerPath]
) -> None:
    """Make sure the index knows every skill a career path names.

    The engine loads the skills its catalogue mentions. A career path may ask
    for one no course teaches yet — a skill only an employer asks for — and it
    must still resolve to the same slug her own skills are keyed on.
    """
    slugs = {slug for path in paths for slug in path.skill_slugs}
    if not slugs:
        return
    extra = await skill_service.SkillIndex.load(session, slugs)
    for alias, skill in extra.by_alias.items():
        ctx.index.by_alias.setdefault(alias, skill)


@dataclass(slots=True)
class HerRecords:
    """Her own records beyond the engine's context. Loaded only for her."""

    statuses: dict[str, SkillStatus] = field(default_factory=dict)
    #: Opportunity ids she applied to and did not take back.
    applied: set[uuid.UUID] = field(default_factory=set)


async def her_records(session: AsyncSession, user_id: uuid.UUID) -> HerRecords:
    records = await skill_service.user_skills(session, user_id)
    applied = await session.scalars(
        select(Application.opportunity_id).where(
            Application.user_id == user_id, Application.status.not_in(_NOT_SENT)
        )
    )
    return HerRecords(
        statuses={record.skill.slug: record.status for record in records if record.status},
        applied=set(applied),
    )


async def context_for(
    session: AsyncSession, user_id: uuid.UUID | None, paths: Iterable[CareerPath]
) -> engine.LearnerContext:
    """Her engine context — or the bare catalogue for a visitor — with the
    career paths' skills resolvable."""
    ctx = (
        await engine.load_context(session, user_id)
        if user_id is not None
        else await _anonymous_context(session)
    )
    await _include(session, ctx, paths)
    return ctx


# ---------------------------------------------------------------------------
# Reading one path against the catalogue
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Reading:
    """One career path matched against the live catalogue."""

    path: CareerPath
    refs: list[SkillRef]
    keys: list[str]
    programs: list[Program]
    tasks: list[PracticalTask]
    opportunities: list[Opportunity]
    #: key -> (courses teaching it, tasks practising it, listings asking for it)
    reach: dict[str, tuple[int, int, int]]
    learning_path: LearningPath | None


def keys_of(ctx: engine.LearnerContext, labels: Iterable[str]) -> set[str]:
    return {ctx.index.key(label) for label in labels if label and label.strip()}


def leads_to(path: CareerPath, opportunity: Opportunity) -> bool:
    """Whether a listing is the kind this direction ends in."""
    return not path.opportunity_types or opportunity.type in path.opportunity_types


def _read(ctx: engine.LearnerContext, path: CareerPath) -> _Reading:
    refs = ctx.index.refs(path.skill_slugs)
    keys = [ctx.index.key(ref.slug or ref.label) for ref in refs]
    wanted = set(keys)

    programs = [p for p in ctx.catalogue if wanted & keys_of(ctx, p.skills_taught)]
    tasks = [t for t in ctx.tasks if wanted & keys_of(ctx, t.skills_practised or [])]
    opportunities = [
        o
        for o in ctx.opportunities
        if leads_to(path, o) and wanted & keys_of(ctx, o.required_skills)
    ]

    reach: dict[str, tuple[int, int, int]] = {}
    for key in keys:
        reach[key] = (
            sum(1 for p in programs if key in keys_of(ctx, p.skills_taught)),
            sum(1 for t in tasks if key in keys_of(ctx, t.skills_practised or [])),
            sum(1 for o in opportunities if key in keys_of(ctx, o.required_skills)),
        )

    # Only a learning path the catalogue still publishes is offered.
    learning_path = next((lp for lp in ctx.paths if lp.id == path.learning_path_id), None)
    return _Reading(path, refs, keys, programs, tasks, opportunities, reach, learning_path)


def _dimensions(reading: _Reading) -> list[ScoreDimension]:
    seen: list[ScoreDimension] = []
    for ref in reading.refs:
        for dimension in ref.dimensions:
            if dimension not in seen:
                seen.append(dimension)
    lp = reading.learning_path
    if lp is not None and lp.dimension is not None and lp.dimension not in seen:
        seen.append(lp.dimension)
    return seen


def status_of(ctx: engine.LearnerContext, her: HerRecords | None, key: str) -> SkillStatus | None:
    """Her status on one skill: the skill layer's, or "she listed it" for a
    label on her profile the skill layer has not mirrored yet."""
    if her is None:
        return None
    if key in her.statuses:
        return her.statuses[key]
    return SkillStatus.SELF_REPORTED if key in ctx.held else None


def _card(
    ctx: engine.LearnerContext,
    reading: _Reading,
    *,
    her: HerRecords | None,
    chosen: bool,
) -> CareerPathRead:
    path = reading.path
    fit = None
    if her is not None:
        fit = CareerFit(
            have=sum(1 for key in reading.keys if key in ctx.held),
            total=len(reading.keys),
            chosen=chosen,
        )
    return CareerPathRead(
        id=path.id,
        slug=path.slug,
        title_i18n=path.title_i18n,
        summary_i18n=path.summary_i18n,
        category=path.category,
        level=path.level,
        skills=reading.refs,
        dimensions=_dimensions(reading),
        counts=CareerCounts(
            programs=len(reading.programs),
            tasks=len(reading.tasks),
            opportunities=0 if _minor(ctx, her) else len(reading.opportunities),
        ),
        fit=fit,
    )


def _minor(ctx: engine.LearnerContext, her: HerRecords | None) -> bool:
    """Known to be under 18. An unknown age is treated as the listing pages
    treat it — as a reader like any other."""
    return her is not None and ctx.age is not None and ctx.age < engine.ADULT_AGE


async def catalogue_for(
    session: AsyncSession,
    user_id: uuid.UUID | None,
    *,
    category: CareerCategory | None = None,
    ctx: engine.LearnerContext | None = None,
) -> list[CareerPathRead]:
    """The catalogue, with her fit on each direction when she is signed in."""
    paths = await catalogue(session, category=category)
    if not paths:
        return []
    if ctx is None:
        ctx = await context_for(session, user_id, paths)
    else:
        await _include(session, ctx, paths)

    her = await her_records(session, user_id) if user_id is not None else None
    choice = await choice_of(session, user_id) if user_id is not None else None
    chosen_id = choice.career_path_id if choice else None

    cards = [_card(ctx, _read(ctx, path), her=her, chosen=path.id == chosen_id) for path in paths]

    # The one direction her skills are closest to — offered only while she has
    # not chosen, and only when she holds at least one skill any of them needs.
    if her is not None and chosen_id is None:
        best = max(cards, key=lambda card: card.fit.have if card.fit else 0)
        if best.fit and best.fit.have:
            best.suggested = True
    return cards


# ---------------------------------------------------------------------------
# One path, in stages
# ---------------------------------------------------------------------------


def _program_rank(
    ctx: engine.LearnerContext,
    reading: _Reading,
    her: HerRecords | None,
    program: Program,
) -> tuple[int, int, int]:
    enrollment = ctx.enrollments.get(program.id)
    started = enrollment is not None and enrollment.status in _OPEN_ENROLLMENT
    finished = enrollment is not None and enrollment.status == EnrollmentStatus.COMPLETED
    order = (
        [item.program_id for item in reading.learning_path.items] if reading.learning_path else []
    )
    missing = sum(
        1
        for key in keys_of(ctx, program.skills_taught) & set(reading.keys)
        if status_of(ctx, her, key) not in EVIDENCED
    )
    return (
        0 if started else 2 if finished else 1,
        order.index(program.id) if program.id in order else len(order),
        -missing,
    )


def _task_rank(ctx: engine.LearnerContext, task: PracticalTask) -> tuple[int, int]:
    status = practice_service.current_status(ctx.task_attempts.get(task.id, []))
    order = {
        TaskStatus.NEEDS_IMPROVEMENT: 0,
        TaskStatus.STARTED: 1,
        TaskStatus.SUBMITTED: 2,
        None: 3,
        TaskStatus.PASSED: 4,
    }
    held = sum(1 for label in task.skills_practised or [] if ctx.holds(label))
    return (order.get(status, 3), -held)


def _opportunity_rank(
    ctx: engine.LearnerContext, reading: _Reading, opportunity: Opportunity
) -> tuple[float, int, datetime]:
    _, _, coverage = engine.overlap(ctx.index, ctx.held, opportunity.required_skills)
    on_path = len(keys_of(ctx, opportunity.required_skills) & set(reading.keys))
    return (-coverage, -on_path, opportunity.deadline or _NO_DEADLINE)


def _path_skills(ctx: engine.LearnerContext, reading: _Reading, labels: Iterable[str]) -> int:
    return len(keys_of(ctx, labels) & set(reading.keys))


def _learn_stage(
    ctx: engine.LearnerContext, reading: _Reading, her: HerRecords | None
) -> StageRead:
    teachable = [key for key in reading.keys if reading.reach[key][0]]
    if not teachable and reading.learning_path is None:
        return StageRead(
            stage=JourneyStage.LEARN, status=StageStatus.UNAVAILABLE, reason="no_courses"
        )
    if her is None:
        return StageRead(stage=JourneyStage.LEARN, total=len(teachable))

    covered = [key for key in teachable if status_of(ctx, her, key) in EVIDENCED]
    lp = reading.learning_path
    lp_status = engine.path_progress(ctx, lp).status if lp is not None else None
    if (teachable and len(covered) == len(teachable)) or lp_status == PathStatus.COMPLETED:
        status = StageStatus.DONE
    elif (
        covered
        or lp_status == PathStatus.IN_PROGRESS
        or (lp is not None and lp.id in ctx.path_records)
        or any(
            p.id in ctx.enrollments and ctx.enrollments[p.id].status in _OPEN_ENROLLMENT
            for p in reading.programs
        )
    ):
        status = StageStatus.IN_PROGRESS
    else:
        status = StageStatus.TODO
    return StageRead(
        stage=JourneyStage.LEARN, status=status, done=len(covered), total=len(teachable)
    )


def _practice_stage(
    ctx: engine.LearnerContext, reading: _Reading, her: HerRecords | None
) -> StageRead:
    if not reading.tasks:
        return StageRead(
            stage=JourneyStage.PRACTICE, status=StageStatus.UNAVAILABLE, reason="no_tasks"
        )
    if her is None:
        return StageRead(stage=JourneyStage.PRACTICE, total=len(reading.tasks))

    statuses = [
        practice_service.current_status(ctx.task_attempts.get(task.id, []))
        for task in reading.tasks
    ]
    passed = sum(1 for status in statuses if status == TaskStatus.PASSED)
    if passed:
        status = StageStatus.DONE
    elif any(status is not None for status in statuses):
        status = StageStatus.IN_PROGRESS
    else:
        status = StageStatus.TODO
    return StageRead(
        stage=JourneyStage.PRACTICE, status=status, done=passed, total=len(reading.tasks)
    )


def _build_stage(evidence: CareerEvidence | None) -> StageRead:
    if evidence is None:
        return StageRead(stage=JourneyStage.BUILD)
    shown = len(evidence.certificates) + len(evidence.passed_tasks) + len(evidence.projects)
    if evidence.projects:
        status = StageStatus.DONE
    elif shown:
        status = StageStatus.IN_PROGRESS
    else:
        status = StageStatus.TODO
    return StageRead(stage=JourneyStage.BUILD, status=status, done=shown)


def _explore_stage(
    ctx: engine.LearnerContext, reading: _Reading, her: HerRecords | None, applications: int
) -> StageRead:
    if _minor(ctx, her):
        return StageRead(
            stage=JourneyStage.EXPLORE, status=StageStatus.UNAVAILABLE, reason="adults_only"
        )
    if not reading.opportunities:
        return StageRead(
            stage=JourneyStage.EXPLORE, status=StageStatus.UNAVAILABLE, reason="no_listings"
        )
    total = len(reading.opportunities)
    if her is None:
        return StageRead(stage=JourneyStage.EXPLORE, total=total)
    return StageRead(
        stage=JourneyStage.EXPLORE,
        status=StageStatus.DONE if applications else StageStatus.TODO,
        done=applications,
        total=total,
    )


async def _evidence(
    session: AsyncSession, ctx: engine.LearnerContext, reading: _Reading, user_id: uuid.UUID
) -> CareerEvidence:
    """What her portfolio already shows for this direction.

    Read through the portfolio service, so the career page and the portfolio
    page cannot disagree about what she holds. A certificate, a passed task or a
    project counts when it covers one of the path's skills.
    """
    record = await portfolio_service.portfolio_for(session, user_id)
    wanted = set(reading.keys)

    def touches(refs: Sequence[SkillRef]) -> bool:
        return bool(wanted & {ctx.index.key(ref.slug or ref.label) for ref in refs})

    return CareerEvidence(
        certificates=[
            {"title_i18n": item.program_title_i18n, "serial_number": item.serial_number}
            for item in record.certificates
            if touches(item.skills)
        ],
        passed_tasks=[
            {"slug": item.task_slug, "title_i18n": item.title_i18n}
            for item in record.practice
            if item.status == TaskStatus.PASSED and touches(item.skills)
        ],
        projects=[
            {"id": str(project.id), "title": project.title}
            for project in record.projects
            if touches(project.skills)
        ],
    )


def _dimension_notes(ctx: engine.LearnerContext, reading: _Reading) -> list[DimensionNote]:
    """Dimensions this work builds that her score says need her.

    Read from the score she already has; the score itself is untouched. Only
    the two weakest, because a list of every area she could improve is not
    advice.
    """
    notes = []
    for dimension in _dimensions(reading):
        score = ctx.scores.get(dimension)
        if score is None:
            continue
        band = band_for(score.current)
        if band != DimensionBand.STRONG:
            notes.append(DimensionNote(dimension=dimension, score=round(score.current), band=band))
    notes.sort(key=lambda note: note.score)
    return notes[:2]


def _next_step(
    ctx: engine.LearnerContext,
    reading: _Reading,
    her: HerRecords,
    current: JourneyStage | None,
    programs: list[Program],
    tasks: list[PracticalTask],
    evidence: CareerEvidence,
) -> NextStep | None:
    """One thing to do now, on the stage she is on — in the engine's vocabulary."""
    if current == JourneyStage.LEARN:
        started = [
            p
            for p in programs
            if p.id in ctx.enrollments and ctx.enrollments[p.id].status in _OPEN_ENROLLMENT
        ]
        if started:
            return engine._continue_step(ctx, started[0])

        lp = reading.learning_path
        if lp is not None:
            progress = engine.path_progress(ctx, lp)
            if progress.status != PathStatus.COMPLETED:
                walking = lp.id in ctx.path_records or progress.status == PathStatus.IN_PROGRESS
                reason = (
                    RecommendationReason.IN_PROGRESS
                    if walking
                    else RecommendationReason.CAREER_SKILL
                )
                params = {"progress": progress.percent}
                return NextStep(
                    kind=NextStepKind.CONTINUE_PATH if walking else NextStepKind.START_PATH,
                    reason=reason,
                    params=params,
                    path=engine._path_suggestion(ctx, lp, progress, reason=reason, params=params),
                )

        fresh = next(
            (
                p
                for p in programs
                if p.id not in ctx.enrollments
                and any(
                    status_of(ctx, her, key) not in EVIDENCED
                    for key in keys_of(ctx, p.skills_taught) & set(reading.keys)
                )
            ),
            None,
        )
        if fresh is not None:
            params = {"skills": _path_skills(ctx, reading, fresh.skills_taught)}
            return NextStep(
                kind=NextStepKind.START_PROGRAM,
                reason=RecommendationReason.CAREER_SKILL,
                params=params,
                program=engine._program_suggestion(
                    ctx, fresh, reason=RecommendationReason.CAREER_SKILL, params=params
                ),
            )
        return None

    if current == JourneyStage.PRACTICE:
        for task in tasks:
            status = practice_service.current_status(ctx.task_attempts.get(task.id, []))
            if status == TaskStatus.PASSED:
                continue
            if status == TaskStatus.NEEDS_IMPROVEMENT:
                kind, reason = NextStepKind.IMPROVE_TASK, RecommendationReason.NEEDS_IMPROVEMENT
            elif status is not None:
                kind, reason = NextStepKind.PRACTISE_TASK, RecommendationReason.IN_PROGRESS
            else:
                kind, reason = NextStepKind.PRACTISE_TASK, RecommendationReason.CAREER_SKILL
            return NextStep(
                kind=kind,
                reason=reason,
                task=engine._task_suggestion(ctx, task, reason=reason),
            )
        return None

    if current == JourneyStage.BUILD:
        return NextStep(
            kind=NextStepKind.ADD_PROJECT,
            reason=RecommendationReason.EVIDENCE_TO_SHOW,
            params={
                "certificates": len(evidence.certificates),
                "passed": len(evidence.passed_tasks),
            },
        )

    if current == JourneyStage.EXPLORE:
        return NextStep(
            kind=NextStepKind.EXPLORE_OPPORTUNITIES,
            reason=RecommendationReason.CAREER_SKILL,
            params={"count": len(reading.opportunities)},
            opportunity_types=list(dict.fromkeys(o.type for o in reading.opportunities)),
        )
    return None


async def detail_for(
    session: AsyncSession,
    path: CareerPath,
    user_id: uuid.UUID | None,
    *,
    ctx: engine.LearnerContext | None = None,
) -> CareerPathDetail:
    """One direction in stages — and, for her, where she stands on each."""
    if ctx is None:
        ctx = await context_for(session, user_id, [path])
    else:
        await _include(session, ctx, [path])

    reading = _read(ctx, path)
    her = await her_records(session, user_id) if user_id is not None else None
    choice = await choice_of(session, user_id) if user_id is not None else None
    chosen = choice is not None and choice.career_path_id == path.id

    programs = sorted(reading.programs, key=lambda p: _program_rank(ctx, reading, her, p))
    tasks = sorted(reading.tasks, key=lambda t: _task_rank(ctx, t))
    minor = _minor(ctx, her)
    opportunities = (
        []
        if minor
        else sorted(reading.opportunities, key=lambda o: _opportunity_rank(ctx, reading, o))
    )

    evidence = await _evidence(session, ctx, reading, user_id) if user_id is not None else None
    applications = (
        sum(1 for o in reading.opportunities if o.id in her.applied) if her is not None else 0
    )

    stages = [
        _learn_stage(ctx, reading, her),
        _practice_stage(ctx, reading, her),
        _build_stage(evidence),
        _explore_stage(ctx, reading, her, applications),
    ]

    card = _card(ctx, reading, her=her, chosen=chosen)
    detail = CareerPathDetail(
        **card.model_dump(),
        description_i18n=path.description_i18n,
        learning_path=(
            engine._path_suggestion(
                ctx,
                reading.learning_path,
                engine.path_progress(ctx, reading.learning_path),
                reason=RecommendationReason.CAREER_SKILL,
            )
            if reading.learning_path is not None
            else None
        ),
        skill_details=[
            CareerSkill(
                skill=ref,
                status=status_of(ctx, her, key),
                programs=reading.reach[key][0],
                tasks=reading.reach[key][1],
                opportunities=0 if minor else reading.reach[key][2],
            )
            for ref, key in zip(reading.refs, reading.keys, strict=True)
        ],
        programs=[
            engine._program_suggestion(
                ctx,
                program,
                reason=(
                    RecommendationReason.IN_PROGRESS
                    if program.id in ctx.enrollments
                    and ctx.enrollments[program.id].status in _OPEN_ENROLLMENT
                    else RecommendationReason.CAREER_SKILL
                ),
                params={"skills": _path_skills(ctx, reading, program.skills_taught)},
            )
            for program in programs[:MAX_PROGRAMS]
        ],
        tasks=[
            engine._task_suggestion(
                ctx,
                task,
                reason=RecommendationReason.CAREER_SKILL,
                params={"skills": _path_skills(ctx, reading, task.skills_practised or [])},
            )
            for task in tasks[:MAX_TASKS]
        ],
        opportunities=[
            _opportunity(ctx, reading, opportunity, anonymous=her is None)
            for opportunity in opportunities[:MAX_OPPORTUNITIES]
        ],
        stages=stages,
    )

    if her is None or evidence is None:
        return detail

    current = next(
        (
            stage.stage
            for stage in stages
            if stage.status in (StageStatus.TODO, StageStatus.IN_PROGRESS)
        ),
        None,
    )
    detail.journey = CareerJourney(
        chosen=chosen,
        chosen_at=choice.chosen_at if chosen and choice else None,
        have=card.fit.have if card.fit else 0,
        total=len(reading.keys),
        current=current,
        next_step=_next_step(ctx, reading, her, current, programs, tasks, evidence),
        dimension_notes=_dimension_notes(ctx, reading),
        evidence=evidence,
        applications=applications,
    )
    return detail


def _opportunity(
    ctx: engine.LearnerContext, reading: _Reading, opportunity: Opportunity, *, anonymous: bool
) -> OpportunitySuggestion:
    suggestion = engine._opportunity_suggestion(
        ctx,
        opportunity,
        reason=RecommendationReason.CAREER_SKILL,
        params={"skills": _path_skills(ctx, reading, opportunity.required_skills)},
    )
    # A visitor holds nothing: "0% match" would read as a verdict on somebody
    # who has not signed in. The listing's own skills still show.
    if anonymous:
        suggestion.match = None
    return suggestion


async def my_direction(session: AsyncSession, user_id: uuid.UUID) -> CareerPathDetail | None:
    """Her chosen direction, read in full, or None when she has not chosen."""
    choice = await choice_of(session, user_id)
    if choice is None:
        return None
    path = await session.get(CareerPath, choice.career_path_id)
    if path is None:
        return None
    return await detail_for(session, path, user_id)
