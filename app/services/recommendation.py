"""Recommendations: skill matching, gaps and her next steps.

Matching is computed in code — it is deterministic, explainable and free. The
model is used only to phrase the explanation and to break ties.

This is the one place that decides what she is shown next. The cabinet reads
it; anything else that recommends should read it too rather than grow its own
ranking. Every suggestion comes from her own records and the live catalogue,
carries its reason as data the browser renders in her language, and never
names a course or a listing the database does not hold. No model call: the
cabinet must be fast, and must say the same thing twice in a row.

Skills go through `services.skills`. Two labels are the same skill when they
resolve to the same taxonomy entry, so a woman whose profile says
"бухгалтерия" matches a vacancy asking for "buxgalteriya", and every skill
that reaches the browser carries names in all three languages.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    SCORE_WEIGHTS,
    DimensionBand,
    EnrollmentStatus,
    NextStepKind,
    PlanItemStatus,
    RecommendationReason,
    ScoreDimension,
    TaskStatus,
)
from app.models.assessment import AssessmentAnswer, AssessmentQuestion, DevelopmentScore
from app.models.learning_path import LearningPath, UserLearningPath
from app.models.opportunity import Opportunity
from app.models.plan import DevelopmentPlan, PlanItem
from app.models.portfolio import PortfolioProject
from app.models.practice import PracticalTask, TaskAttempt
from app.models.profile import Profile
from app.models.program import Certificate, Enrollment, Program
from app.models.user import User
from app.schemas.assessment import AnswerInsight, DimensionInsight, ScoreInsightsRead
from app.schemas.learning_path import LearningPathProgress, PathStatus
from app.schemas.opportunity import SkillGap
from app.schemas.recommendation import (
    NextStep,
    OpportunitySuggestion,
    Params,
    PathSuggestion,
    ProgramSuggestion,
    RecommendationsRead,
    TaskSuggestion,
)
from app.schemas.skill import SkillGapRead, SkillRef
from app.services import eligibility
from app.services import learning_path as path_service
from app.services import practice as practice_service
from app.services import skills as skill_service
from app.services.age_gate import age_from_profile
from app.services.score_insights import (
    DIMENSION_OPPORTUNITY_TYPES,
    DIMENSION_PROGRAM_CATEGORIES,
    AnsweredQuestion,
    answer_label,
    band_for,
    focus_order,
    split_answers,
)
from app.services.scoring import composite_score, weakest_dimensions


def _normalise(skill: str) -> str:
    return skill_service.normalise(skill)


def skill_overlap(
    user_skills: list[str], required_skills: list[str]
) -> tuple[list[str], list[str], float]:
    """Return (matched, missing, coverage) by label.

    The plain-string comparison, kept for callers that hold labels and no
    session. Anything with a database open should use `overlap`, which folds
    spellings and languages together through the taxonomy.
    """
    if not required_skills:
        return [], [], 1.0
    user_set = {_normalise(s) for s in user_skills}
    matched, missing = [], []
    for skill in required_skills:
        (matched if _normalise(skill) in user_set else missing).append(skill)
    return matched, missing, round(len(matched) / len(required_skills), 3)


def overlap(
    index: skill_service.SkillIndex, held: set[str], required_skills: list[str]
) -> tuple[list[SkillRef], list[SkillRef], float]:
    """What she covers of a listing's requirements, as skills rather than words."""
    if not required_skills:
        return [], [], 1.0
    matched: list[SkillRef] = []
    missing: list[SkillRef] = []
    for label in required_skills:
        (matched if index.key(label) in held else missing).append(index.ref(label))
    return matched, missing, round(len(matched) / len(required_skills), 3)


async def _held_keys(
    session: AsyncSession, user_id: uuid.UUID, index: skill_service.SkillIndex, labels: list[str]
) -> set[str]:
    """Every skill she can be matched on.

    The skill layer is the source of truth; the labels still written on her
    profile are folded in too, so matching keeps working for an account whose
    skills have not been mirrored across yet.
    """
    held = await skill_service.skill_keys(session, user_id)
    held.update(index.key(label) for label in labels if label and label.strip())
    return held


async def match_opportunities(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    limit: int = 10,
    region: str | None = None,
) -> list[tuple[Opportunity, list[str], list[str], float]]:
    """Rank active opportunities by skill coverage, most relevant first."""
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    user_skills = list(profile.skills) if profile else []

    stmt = select(Opportunity).where(
        Opportunity.is_active.is_(True), Opportunity.starts_at.is_(None)
    )
    if region:
        stmt = stmt.where(Opportunity.region == region)

    # A listing she definitely cannot apply to is not a match to rank —
    # the same rule the catalogue and the apply endpoint read.
    age = age_from_profile(profile)
    opportunities = [
        item
        for item in (await session.execute(stmt.limit(200))).scalars()
        if not eligibility.excluded(item, age=age)
    ]

    labels = [label for item in opportunities for label in item.required_skills]
    index = await skill_service.SkillIndex.load(session, [*labels, *user_skills])
    held = await _held_keys(session, user_id, index, user_skills)

    scored = []
    for opportunity in opportunities:
        matched, missing, coverage = overlap(index, held, opportunity.required_skills)
        scored.append(
            (opportunity, [ref.label for ref in matched], [ref.label for ref in missing], coverage)
        )

    scored.sort(key=lambda row: row[3], reverse=True)
    return scored[:limit]


async def analyse_skill_gap(
    session: AsyncSession, *, user_id: uuid.UUID, opportunity_id: uuid.UUID
) -> SkillGap | None:
    """Compare a user's skills to one vacancy and suggest closing courses."""
    opportunity = await session.get(Opportunity, opportunity_id)
    if opportunity is None:
        return None

    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    user_skills = list(profile.skills) if profile else []
    programs = list(
        (await session.execute(select(Program).where(Program.is_published.is_(True)))).scalars()
    )

    taught = [label for program in programs for label in program.skills_taught]
    index = await skill_service.SkillIndex.load(
        session, [*opportunity.required_skills, *user_skills, *taught]
    )
    held = await _held_keys(session, user_id, index, user_skills)
    matched, missing, coverage = overlap(index, held, opportunity.required_skills)

    recommended: list[uuid.UUID] = []
    if missing:
        wanted = {index.key(ref.label) for ref in missing}
        for program in programs:
            if wanted & {index.key(label) for label in program.skills_taught}:
                recommended.append(program.id)

    return SkillGap(
        opportunity_id=opportunity_id,
        matched_skills=[ref.label for ref in matched],
        missing_skills=[ref.label for ref in missing],
        coverage=coverage,
        recommended_program_ids=recommended[:5],
    )


# ---------------------------------------------------------------------------
# Next steps, suggestions and the score read as actions
# ---------------------------------------------------------------------------

# More than three is a to-do list, and a list of things not yet done is not
# what the cabinet should open on.
MAX_NEXT_STEPS = 3
MAX_SUGGESTIONS = 4
MAX_SKILL_GAPS = 6
# The engine ranks in memory, so what it reads is bounded.
CATALOGUE_LIMIT = 200
ADULT_AGE = 18

_OPEN_ENROLLMENT = (EnrollmentStatus.ENROLLED, EnrollmentStatus.IN_PROGRESS)
_CLOSED_PLAN_ITEM = (PlanItemStatus.DONE, PlanItemStatus.SKIPPED)
_NO_DEADLINE = datetime.max.replace(tzinfo=UTC)
_NEVER = datetime.min.replace(tzinfo=UTC)


@dataclass(slots=True)
class LearnerContext:
    """Everything the engine reads about one woman, loaded once per request."""

    user_id: uuid.UUID
    age: int | None
    region: str | None
    #: The labels written on her profile. Kept as labels because the AI prompt
    #: and the partner payload still read them that way.
    skills: list[str]
    #: Labels in play — hers, the catalogue's, the listings' — resolved to the
    #: skill taxonomy.
    index: skill_service.SkillIndex
    #: What she can be matched on: taxonomy slugs, plus any label of hers the
    #: taxonomy has never seen.
    held: set[str]
    scores: dict[ScoreDimension, DevelopmentScore]
    enrollments: dict[uuid.UUID, Enrollment]
    # Every programme the engine may name: the catalogue, plus anything she is
    # enrolled in, which may since have been unpublished.
    programs: dict[uuid.UUID, Program]
    # Published programmes suitable for her age and region, newest first.
    catalogue: list[Program]
    # Active listings that are still open, nearest deadline first.
    opportunities: list[Opportunity]
    active_plan: DevelopmentPlan | None
    has_draft_plan: bool
    # Published routes through the catalogue, in curation order, and the ones
    # she has said she is on. Where she stands on any of them is read from
    # `enrollments` above — a path keeps no progress of its own.
    paths: list[LearningPath]
    path_records: dict[uuid.UUID, UserLearningPath]
    # Published practical tasks, and her own attempts at them by task id.
    # Practice is offered from the same place everything else is: her records
    # and the live catalogue, with no second ranking of its own.
    tasks: list[PracticalTask]
    task_attempts: dict[uuid.UUID, list[TaskAttempt]]
    # What she could put in a portfolio, and whether she has written any of
    # it up. Counted, not loaded: the engine only needs to know "any".
    certificates: int = 0
    projects: int = 0
    # Events she could still register for, soonest first. Kept apart from
    # `opportunities`: an event is recommended for what it covers and when it
    # is, not for how well her skills cover what it asks (`services.events`).
    events: list[Opportunity] = field(default_factory=list)

    @property
    def current(self) -> dict[ScoreDimension, float]:
        return {dimension: score.current for dimension, score in self.scores.items()}

    def holds(self, label: str) -> bool:
        return self.index.key(label) in self.held


def program_fits(program: Program, *, age: int | None, region: str | None) -> bool:
    """Whether a published programme may be offered to her at all.

    An unknown age is not an adult one: a programme that starts at 18 is not
    suggested until her age is known — the same protective reading the health
    gate takes. A region list restricts only when her region is known too; an
    empty list means everywhere.
    """
    if program.target_regions and region and region not in program.target_regions:
        return False
    if age is None:
        return (program.target_age_min or 0) < ADULT_AGE
    if program.target_age_min is not None and age < program.target_age_min:
        return False
    return not (program.target_age_max is not None and age > program.target_age_max)


async def load_context(
    session: AsyncSession, user_id: uuid.UUID, *, now: datetime | None = None
) -> LearnerContext:
    """Her records and the live catalogue, in a fixed and small number of queries."""
    now = now or datetime.now(UTC)

    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    region = await session.scalar(select(User.region).where(User.id == user_id))
    scores = {
        row.dimension: row
        for row in (
            await session.execute(
                select(DevelopmentScore).where(DevelopmentScore.user_id == user_id)
            )
        ).scalars()
    }
    enrolled = (
        await session.execute(
            select(Enrollment, Program)
            .join(Program, Program.id == Enrollment.program_id)
            .where(Enrollment.user_id == user_id)
        )
    ).all()
    published = list(
        (
            await session.execute(
                select(Program)
                .where(Program.is_published.is_(True))
                .order_by(Program.published_at.desc().nullslast())
                .limit(CATALOGUE_LIMIT)
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
                    # An event that has begun takes no more registrations.
                    or_(Opportunity.starts_at.is_(None), Opportunity.starts_at > now),
                )
                .order_by(Opportunity.deadline.asc().nullslast())
                .limit(CATALOGUE_LIMIT)
            )
        ).scalars()
    )
    active_plan = await session.scalar(
        select(DevelopmentPlan).where(
            DevelopmentPlan.user_id == user_id, DevelopmentPlan.is_active.is_(True)
        )
    )
    draft = await session.scalar(
        select(DevelopmentPlan.id)
        .where(
            DevelopmentPlan.user_id == user_id,
            DevelopmentPlan.is_active.is_(False),
            DevelopmentPlan.accepted_at.is_(None),
        )
        .limit(1)
    )

    paths = await path_service.catalogue(session)
    path_records = await path_service.records_for(session, user_id, [path.id for path in paths])

    tasks = await practice_service.catalogue(session)
    task_attempts = await practice_service.attempts_for(
        session, user_id, [task.id for task in tasks]
    )
    certificates = (
        await session.scalar(
            select(func.count())
            .select_from(Certificate)
            .where(Certificate.user_id == user_id, Certificate.revoked_at.is_(None))
        )
    ) or 0
    projects = (
        await session.scalar(
            select(func.count())
            .select_from(PortfolioProject)
            .where(PortfolioProject.user_id == user_id)
        )
    ) or 0

    age = age_from_profile(profile)
    # Listings she definitely cannot apply to — an age rule that excludes her,
    # or any listing at all for a girl known to be under 18 — are not offered
    # anywhere the engine reaches: next steps, suggestions, the Coach, the
    # career page. The catalogue and the apply endpoint read the same rule.
    opportunities = [
        item for item in opportunities if not eligibility.excluded(item, age=age, now=now)
    ]
    events = sorted(
        (item for item in opportunities if item.starts_at is not None),
        key=lambda item: item.starts_at,
    )
    opportunities = [item for item in opportunities if item.starts_at is None]
    region_value = getattr(region, "value", region)
    programs = {program.id: program for program in published}
    programs.update({program.id: program for _, program in enrolled})
    profile_skills = list(profile.skills) if profile else []

    labels = {label for program in programs.values() for label in program.skills_taught}
    labels.update(label for item in opportunities for label in item.required_skills)
    labels.update(label for item in events for label in item.required_skills)
    labels.update(profile_skills)
    # A path's courses may sit outside the catalogue she is offered — an age
    # range, a region — and a route still has to be able to name what it
    # teaches.
    labels.update(label for path in paths for label in path_service.path_labels(path))
    labels.update(label for task in tasks for label in (task.skills_practised or []))
    index = await skill_service.SkillIndex.load(session, labels)

    return LearnerContext(
        user_id=user_id,
        age=age,
        region=region_value,
        skills=profile_skills,
        index=index,
        held=await _held_keys(session, user_id, index, profile_skills),
        scores=scores,
        enrollments={enrollment.program_id: enrollment for enrollment, _ in enrolled},
        programs=programs,
        catalogue=[p for p in published if program_fits(p, age=age, region=region_value)],
        opportunities=opportunities,
        active_plan=active_plan,
        has_draft_plan=draft is not None,
        paths=paths,
        path_records=path_records,
        tasks=tasks,
        task_attempts=task_attempts,
        certificates=int(certificates),
        projects=int(projects),
        events=events,
    )


# --- building blocks --------------------------------------------------------


def _score_params(ctx: LearnerContext, dimension: ScoreDimension) -> Params:
    score = ctx.scores.get(dimension)
    return {"score": round(score.current)} if score else {}


def _program_suggestion(
    ctx: LearnerContext,
    program: Program,
    *,
    reason: RecommendationReason,
    dimension: ScoreDimension | None = None,
    params: Params | None = None,
) -> ProgramSuggestion:
    enrollment = ctx.enrollments.get(program.id)
    return ProgramSuggestion(
        id=program.id,
        slug=program.slug,
        title_i18n=program.title_i18n,
        category=program.category,
        duration_weeks=program.duration_weeks,
        has_certificate=program.has_certificate,
        skills_taught=ctx.index.refs(program.skills_taught),
        new_skills=ctx.index.refs(s for s in program.skills_taught if not ctx.holds(s)),
        enrollment_status=enrollment.status if enrollment else None,
        progress_percent=enrollment.progress_percent if enrollment else None,
        dimension=dimension,
        reason=reason,
        params=params or {},
    )


def _opportunity_suggestion(
    ctx: LearnerContext,
    opportunity: Opportunity,
    *,
    reason: RecommendationReason,
    dimension: ScoreDimension | None = None,
    params: Params | None = None,
) -> OpportunitySuggestion:
    matched, missing, coverage = overlap(ctx.index, ctx.held, opportunity.required_skills)
    return OpportunitySuggestion(
        id=opportunity.id,
        type=opportunity.type,
        title_i18n=opportunity.title_i18n,
        organisation=opportunity.organisation,
        region=opportunity.region,
        deadline=opportunity.deadline,
        match=coverage if opportunity.required_skills else None,
        matched_skills=matched,
        missing_skills=missing,
        dimension=dimension,
        reason=reason,
        params=params or {},
    )


def _continue_step(
    ctx: LearnerContext, program: Program, dimension: ScoreDimension | None = None
) -> NextStep:
    progress = ctx.enrollments[program.id].progress_percent
    params: Params = {"progress": progress}
    # Enrolled and never opened is not "0% done": that reads as a failure.
    reason = RecommendationReason.IN_PROGRESS if progress > 0 else RecommendationReason.ENROLLED
    return NextStep(
        kind=NextStepKind.CONTINUE_PROGRAM,
        reason=reason,
        dimension=dimension,
        params=params,
        program=_program_suggestion(
            ctx, program, reason=reason, dimension=dimension, params=params
        ),
    )


def _start_step(ctx: LearnerContext, program: Program, dimension: ScoreDimension) -> NextStep:
    params = _score_params(ctx, dimension)
    return NextStep(
        kind=NextStepKind.START_PROGRAM,
        reason=RecommendationReason.FOCUS_DIMENSION,
        dimension=dimension,
        params=params,
        program=_program_suggestion(
            ctx,
            program,
            reason=RecommendationReason.FOCUS_DIMENSION,
            dimension=dimension,
            params=params,
        ),
    )


def path_progress(ctx: LearnerContext, path: LearningPath) -> LearningPathProgress:
    """Where she stands on a route, from the enrollments already loaded.

    The same computation the path endpoints run, called on the same data —
    which is the point of it living in one service. The engine adds no second
    reading of her progress.
    """
    return path_service.progress_of(
        path_service.item_states(path.items, ctx.enrollments), ctx.path_records.get(path.id)
    )


def _path_suggestion(
    ctx: LearnerContext,
    path: LearningPath,
    progress: LearningPathProgress,
    *,
    reason: RecommendationReason,
    params: Params | None = None,
) -> PathSuggestion:
    labels = path_service.path_labels(path)
    return PathSuggestion(
        id=path.id,
        slug=path.slug,
        title_i18n=path.title_i18n,
        dimension=path.dimension,
        level=path.level,
        program_count=len(path.items),
        completed_count=progress.completed_items,
        percent=progress.percent,
        started=path.id in ctx.path_records,
        new_skills=ctx.index.refs(label for label in labels if not ctx.holds(label)),
        reason=reason,
        params=params or {},
    )


def open_paths(ctx: LearnerContext) -> list[tuple[LearningPath, LearningPathProgress]]:
    """Routes she is already walking, furthest along first.

    "Already walking" counts an enrollment in one of the courses as well as a
    path she named: the progress is real either way, and a route she is a third
    of the way through is better advice than a route she has not touched.
    """
    walking = [
        (path, progress)
        for path in ctx.paths
        for progress in [path_progress(ctx, path)]
        if progress.status == PathStatus.IN_PROGRESS
    ]
    walking.sort(key=lambda row: (0 if row[0].id in ctx.path_records else 1, -row[1].percent))
    return walking


def paths_for_dimension(ctx: LearnerContext, dimension: ScoreDimension) -> list[LearningPath]:
    """Untouched routes that build one dimension, the most to teach her first."""
    candidates = [
        path
        for path in ctx.paths
        if path.dimension == dimension and path_progress(ctx, path).status == PathStatus.NOT_STARTED
    ]
    return sorted(
        candidates,
        key=lambda path: (
            -sum(1 for label in path_service.path_labels(path) if not ctx.holds(label))
        ),
    )


def path_suggestions(ctx: LearnerContext, limit: int = MAX_SUGGESTIONS) -> list[PathSuggestion]:
    """Routes worth her attention: the ones she is on, then the ones she needs.

    Empty until she has a score *and* the catalogue holds a path — the cabinet
    never shows a route that does not exist, and an unranked list of every path
    is the catalogue page again.
    """
    suggestions = [
        _path_suggestion(
            ctx,
            path,
            progress,
            reason=RecommendationReason.IN_PROGRESS,
            params={"progress": progress.percent},
        )
        for path, progress in open_paths(ctx)[:limit]
    ]
    named = {suggestion.id for suggestion in suggestions}

    for dimension in focus_dimensions(ctx):
        if len(suggestions) >= limit:
            break
        for path in paths_for_dimension(ctx, dimension):
            if path.id in named:
                continue
            named.add(path.id)
            suggestions.append(
                _path_suggestion(
                    ctx,
                    path,
                    path_progress(ctx, path),
                    reason=RecommendationReason.FOCUS_DIMENSION,
                    params=_score_params(ctx, dimension),
                )
            )
            break

    return suggestions[:limit]


def _path_step(ctx: LearnerContext) -> NextStep | None:
    """One route to put in front of her, or nothing.

    A path she is part-way through comes first — finishing something started
    beats starting something new. Only then, and only once her score says
    where she needs it, a route for the area that needs her most.
    """
    for path, progress in open_paths(ctx):
        params: Params = {"progress": progress.percent}
        return NextStep(
            kind=NextStepKind.CONTINUE_PATH,
            reason=RecommendationReason.IN_PROGRESS,
            dimension=path.dimension,
            params=params,
            path=_path_suggestion(
                ctx, path, progress, reason=RecommendationReason.IN_PROGRESS, params=params
            ),
        )

    if not ctx.scores:
        return None

    for dimension in focus_dimensions(ctx):
        for path in paths_for_dimension(ctx, dimension):
            params = _score_params(ctx, dimension)
            return NextStep(
                kind=NextStepKind.START_PATH,
                reason=RecommendationReason.FOCUS_DIMENSION,
                dimension=dimension,
                params=params,
                path=_path_suggestion(
                    ctx,
                    path,
                    path_progress(ctx, path),
                    reason=RecommendationReason.FOCUS_DIMENSION,
                    params=params,
                ),
            )
    return None


def _task_status(ctx: LearnerContext, task: PracticalTask) -> str | None:
    """Where she stands on a task, from her own attempts."""
    status = practice_service.current_status(ctx.task_attempts.get(task.id, []))
    return status.value if status else None


def _task_suggestion(
    ctx: LearnerContext,
    task: PracticalTask,
    *,
    reason: RecommendationReason,
    params: Params | None = None,
) -> TaskSuggestion:
    return TaskSuggestion(
        id=task.id,
        slug=task.slug,
        title_i18n=task.title_i18n,
        level=task.level,
        estimated_minutes=task.estimated_minutes,
        skills=ctx.index.refs(task.skills_practised or []),
        status=_task_status(ctx, task),
        reason=reason,
        params=params or {},
    )


def unfinished_tasks(ctx: LearnerContext) -> list[PracticalTask]:
    """Work she started and has not finished — something to fix first."""
    wanted = (TaskStatus.NEEDS_IMPROVEMENT, TaskStatus.STARTED)
    ranked = [
        (wanted.index(status), task)
        for task in ctx.tasks
        for status in [practice_service.current_status(ctx.task_attempts.get(task.id, []))]
        if status in wanted
    ]
    ranked.sort(key=lambda row: row[0])
    return [task for _, task in ranked]


def tasks_for_practice(ctx: LearnerContext) -> list[PracticalTask]:
    """Tasks worth starting: the ones that practise something she was taught.

    A task she has already passed is never offered again, and neither is one
    she is part-way through — that one is in `unfinished_tasks`. Ranked by how
    much of what it practises she already holds, because practice is what you
    do with something you have learned, not a way of learning it cold.
    """
    open_or_done = {
        task.id
        for task in ctx.tasks
        if practice_service.current_status(ctx.task_attempts.get(task.id, [])) is not None
    }
    ranked: list[tuple[int, PracticalTask]] = []
    for task in ctx.tasks:
        if task.id in open_or_done:
            continue
        labels = task.skills_practised or []
        if not labels:
            continue
        held = sum(1 for label in labels if ctx.holds(label))
        if held:
            ranked.append((-held, task))
    ranked.sort(key=lambda row: row[0])
    return [task for _, task in ranked]


def task_suggestions(ctx: LearnerContext, limit: int = MAX_SUGGESTIONS) -> list[TaskSuggestion]:
    """Practice worth her attention: unfinished first, then what she can apply."""
    out = [
        _task_suggestion(ctx, task, reason=RecommendationReason.IN_PROGRESS)
        for task in unfinished_tasks(ctx)[:limit]
    ]
    named = {row.id for row in out}
    for task in tasks_for_practice(ctx):
        if len(out) >= limit:
            break
        if task.id in named:
            continue
        out.append(_task_suggestion(ctx, task, reason=RecommendationReason.SKILLS_MATCH))
    return out[:limit]


def task_step(ctx: LearnerContext) -> NextStep | None:
    """One piece of practice to put in front of her, or nothing.

    Something she started and has not finished leads — a task marked "needs
    improvement" is the most concrete thing on the platform, because somebody
    wrote down exactly what to change. Only then a fresh one.
    """
    for task in unfinished_tasks(ctx):
        status = practice_service.current_status(ctx.task_attempts.get(task.id, []))
        needs_fixing = status == TaskStatus.NEEDS_IMPROVEMENT
        reason = (
            RecommendationReason.NEEDS_IMPROVEMENT
            if needs_fixing
            else RecommendationReason.IN_PROGRESS
        )
        return NextStep(
            kind=NextStepKind.IMPROVE_TASK if needs_fixing else NextStepKind.PRACTISE_TASK,
            reason=reason,
            params={},
            task=_task_suggestion(ctx, task, reason=reason),
        )

    for task in tasks_for_practice(ctx):
        return NextStep(
            kind=NextStepKind.PRACTISE_TASK,
            reason=RecommendationReason.SKILLS_MATCH,
            params={},
            task=_task_suggestion(ctx, task, reason=RecommendationReason.SKILLS_MATCH),
        )
    return None


def focus_dimensions(ctx: LearnerContext, limit: int = 3) -> list[ScoreDimension]:
    """The dimensions that need her most, strong ones left out."""
    current = ctx.current
    return [d for d in focus_order(current) if band_for(current[d]) != DimensionBand.STRONG][:limit]


def programs_for_dimension(ctx: LearnerContext, dimension: ScoreDimension) -> list[Program]:
    """Programmes that build a dimension, best first. Finished ones are left out.

    A course she has started comes first, furthest along first; then the
    category most directly tied to the dimension; then whichever would teach
    her the most she does not hold yet.
    """
    categories = DIMENSION_PROGRAM_CATEGORIES[dimension]

    def rank(program: Program) -> tuple[int, int, int, int]:
        enrollment = ctx.enrollments.get(program.id)
        started = enrollment is not None and enrollment.status in _OPEN_ENROLLMENT
        progress = enrollment.progress_percent if enrollment is not None and started else 0
        new = sum(1 for label in program.skills_taught if not ctx.holds(label))
        return (0 if started else 1, -progress, categories.index(program.category), -new)

    candidates = [
        program
        for program in ctx.catalogue
        if program.category in categories
        and not (
            program.id in ctx.enrollments
            and ctx.enrollments[program.id].status == EnrollmentStatus.COMPLETED
        )
    ]
    return sorted(candidates, key=rank)


def matching_opportunities(ctx: LearnerContext) -> list[Opportunity]:
    """Open listings her skills cover at least in part, best covered first.

    A listing that names no skills is not a match. Full coverage of nothing is
    right for a gap analysis and wrong for a ranking: it would put every grant
    that asks for nothing above a vacancy she is half qualified for.
    """
    ranked: list[tuple[float, Opportunity]] = []
    for opportunity in ctx.opportunities:
        if not opportunity.required_skills:
            continue
        matched, _, coverage = overlap(ctx.index, ctx.held, opportunity.required_skills)
        if matched:
            ranked.append((coverage, opportunity))
    ranked.sort(key=lambda row: (-row[0], row[1].deadline or _NO_DEADLINE))
    return [opportunity for _, opportunity in ranked]


def _interleave(
    pools: Sequence[tuple[ScoreDimension, Sequence[Any]]], limit: int, seen: set[uuid.UUID]
) -> list[tuple[ScoreDimension, Any]]:
    """One item from each dimension in turn, so no single area fills the list."""
    queues = [(dimension, list(items)) for dimension, items in pools]
    picked: list[tuple[ScoreDimension, Any]] = []
    while len(picked) < limit and any(items for _, items in queues):
        for dimension, items in queues:
            while items and items[0].id in seen:
                items.pop(0)
            if items:
                item = items.pop(0)
                seen.add(item.id)
                picked.append((dimension, item))
                if len(picked) >= limit:
                    break
    return picked


# --- what the cabinet reads -------------------------------------------------


def program_suggestions(
    ctx: LearnerContext, limit: int = MAX_SUGGESTIONS
) -> list[ProgramSuggestion]:
    """Courses she has not started, drawn in turn from the dimensions that need her most.

    Empty until she has a score: without one there is nothing personal to rank
    by, and an unranked list would just be the catalogue page again.
    """
    pools = [
        (
            dimension,
            [p for p in programs_for_dimension(ctx, dimension) if p.id not in ctx.enrollments],
        )
        for dimension in focus_dimensions(ctx)
    ]
    return [
        _program_suggestion(
            ctx,
            program,
            reason=RecommendationReason.FOCUS_DIMENSION,
            dimension=dimension,
            params=_score_params(ctx, dimension),
        )
        for dimension, program in _interleave(pools, limit, set())
    ]


def opportunity_suggestions(
    ctx: LearnerContext, limit: int = MAX_SUGGESTIONS
) -> list[OpportunitySuggestion]:
    """Listings her skills match first, then listings that act on her focus dimensions."""
    matched = matching_opportunities(ctx)[:limit]
    suggestions = [
        _opportunity_suggestion(ctx, opportunity, reason=RecommendationReason.SKILLS_MATCH)
        for opportunity in matched
    ]
    pools = [
        (
            dimension,
            [o for o in ctx.opportunities if o.type in DIMENSION_OPPORTUNITY_TYPES[dimension]],
        )
        for dimension in focus_dimensions(ctx)
    ]
    for dimension, opportunity in _interleave(
        pools, limit - len(suggestions), {o.id for o in matched}
    ):
        suggestions.append(
            _opportunity_suggestion(
                ctx,
                opportunity,
                reason=RecommendationReason.FOCUS_DIMENSION,
                dimension=dimension,
                params=_score_params(ctx, dimension),
            )
        )
    return suggestions


def dimension_actions(ctx: LearnerContext, dimension: ScoreDimension) -> list[NextStep]:
    """What would move one dimension: a route through it, the course she is in,
    a new one, open listings.

    The route leads when there is one. "Improve this area through this path" is
    a better answer than a single course, and it is grounded in the same
    records: real paths, real courses, her real enrollments.
    """
    actions: list[NextStep] = []

    for path in paths_for_dimension(ctx, dimension)[:1]:
        params = _score_params(ctx, dimension)
        actions.append(
            NextStep(
                kind=NextStepKind.START_PATH,
                reason=RecommendationReason.FOCUS_DIMENSION,
                dimension=dimension,
                params=params,
                path=_path_suggestion(
                    ctx,
                    path,
                    path_progress(ctx, path),
                    reason=RecommendationReason.FOCUS_DIMENSION,
                    params=params,
                ),
            )
        )

    programs = programs_for_dimension(ctx, dimension)

    started = next(
        (
            p
            for p in programs
            if p.id in ctx.enrollments and ctx.enrollments[p.id].status in _OPEN_ENROLLMENT
        ),
        None,
    )
    if started is not None:
        actions.append(_continue_step(ctx, started, dimension))

    fresh = next((p for p in programs if p.id not in ctx.enrollments), None)
    if fresh is not None:
        actions.append(_start_step(ctx, fresh, dimension))

    types = DIMENSION_OPPORTUNITY_TYPES[dimension]
    listed = [o for o in ctx.opportunities if o.type in types]
    if listed:
        matched_ids = {o.id for o in matching_opportunities(ctx)}
        matched = [o for o in listed if o.id in matched_ids]
        pool = matched or listed
        actions.append(
            NextStep(
                kind=NextStepKind.EXPLORE_OPPORTUNITIES,
                reason=(
                    RecommendationReason.SKILLS_MATCH
                    if matched
                    else RecommendationReason.FOCUS_DIMENSION
                ),
                dimension=dimension,
                params={"count": len(pool), **({} if matched else _score_params(ctx, dimension))},
                opportunity_types=[kind for kind in types if any(o.type == kind for o in pool)],
            )
        )
    return actions


def _pending_plan_item(ctx: LearnerContext) -> PlanItem | None:
    """The next step of the plan she confirmed: earliest due, then plan order."""
    if ctx.active_plan is None:
        return None
    pending = [item for item in ctx.active_plan.items if item.status not in _CLOSED_PLAN_ITEM]
    if not pending:
        return None
    return min(pending, key=lambda item: (item.due_date or date.max, item.order_index))


def _open_enrollments(ctx: LearnerContext) -> list[Enrollment]:
    """Courses she is in, furthest along first, then most recently touched."""
    return sorted(
        (e for e in ctx.enrollments.values() if e.status in _OPEN_ENROLLMENT),
        key=lambda e: (
            -e.progress_percent,
            -(e.last_activity_at or e.started_at or _NEVER).timestamp(),
        ),
    )


def next_steps(ctx: LearnerContext, limit: int = MAX_NEXT_STEPS) -> list[NextStep]:
    """The few things worth doing next, most important first.

    The order is an argument about whose decisions come first:

    1. Without a score nothing else is personal, so the assessment leads.
    2. A step from the plan she confirmed — she already chose it.
    3. A course she has started, before any new one.
    4. A route she is part-way along, or one for the area that needs her most.
    5. Practice: work she has been told how to fix, then work she can apply.
    6. A new course for the dimension that needs her most.
    7. The plan itself, when she has none yet or one is waiting for her.
    8. Listings her skills already match.
    9. Writing up work she has already done, when she has done some.

    A programme appears once, under whichever rule reached it first.
    """
    steps: list[NextStep] = []
    offered: set[uuid.UUID] = set()

    def add(step: NextStep) -> bool:
        if step.program is not None:
            if step.program.id in offered:
                return False
            offered.add(step.program.id)
        steps.append(step)
        return True

    if not ctx.scores:
        add(NextStep(kind=NextStepKind.TAKE_ASSESSMENT, reason=RecommendationReason.NO_ASSESSMENT))

    item = _pending_plan_item(ctx)
    if item is not None:
        program = ctx.programs.get(item.program_id) if item.program_id else None
        add(
            NextStep(
                kind=NextStepKind.PLAN_ITEM,
                reason=RecommendationReason.IN_YOUR_PLAN,
                dimension=item.dimension,
                plan_item_id=item.id,
                text=item.action,
                due_date=item.due_date,
                program=(
                    _program_suggestion(
                        ctx,
                        program,
                        reason=RecommendationReason.IN_YOUR_PLAN,
                        dimension=item.dimension,
                    )
                    if program is not None
                    else None
                ),
            )
        )

    for enrollment in _open_enrollments(ctx):
        program = ctx.programs.get(enrollment.program_id)
        if program is not None and add(_continue_step(ctx, program)):
            break

    # A route, between the course she is on and a new one. It answers the
    # question a single course cannot — what comes after this — and it is
    # skipped entirely when the catalogue holds no path for her, which is why
    # nothing above or below it changed order.
    path_step = _path_step(ctx)
    if path_step is not None:
        add(path_step)

    # Practice, after the learning it applies. Work she has already started and
    # been told how to fix is the most concrete thing the platform can offer,
    # so it enters here rather than at the end — and, like the path rule above,
    # it is skipped entirely when the catalogue holds no task for her, which is
    # what keeps the existing order unchanged for a platform without any.
    practice = task_step(ctx)
    if practice is not None:
        add(practice)

    if ctx.scores:
        for dimension in focus_dimensions(ctx) or focus_order(ctx.current):
            fresh = next(
                (
                    p
                    for p in programs_for_dimension(ctx, dimension)
                    if p.id not in ctx.enrollments and p.id not in offered
                ),
                None,
            )
            if fresh is not None:
                add(_start_step(ctx, fresh, dimension))
                break

        if ctx.active_plan is None:
            add(
                NextStep(kind=NextStepKind.REVIEW_PLAN, reason=RecommendationReason.PLAN_AWAITING)
                if ctx.has_draft_plan
                else NextStep(kind=NextStepKind.CREATE_PLAN, reason=RecommendationReason.NO_PLAN)
            )

    matched = matching_opportunities(ctx)
    if matched:
        add(
            NextStep(
                kind=NextStepKind.EXPLORE_OPPORTUNITIES,
                reason=RecommendationReason.SKILLS_MATCH,
                params={"count": len(matched)},
                opportunity_types=list(dict.fromkeys(o.type for o in matched)),
            )
        )

    # Last, and only when there is something real to show. It never displaces
    # learning — with three steps already ahead of it, it simply does not
    # appear — and it never asks her to document work she has not done.
    project_step = _project_step(ctx)
    if project_step is not None:
        add(project_step)

    return steps[:limit]


def _project_step(ctx: LearnerContext) -> NextStep | None:
    """Write up work she has done, when she has done some and written none."""
    if ctx.projects:
        return None
    passed = sum(
        1
        for attempts in ctx.task_attempts.values()
        if practice_service.current_status(attempts) == TaskStatus.PASSED
    )
    if not passed and not ctx.certificates:
        return None
    return NextStep(
        kind=NextStepKind.ADD_PROJECT,
        reason=RecommendationReason.EVIDENCE_TO_SHOW,
        params={"certificates": ctx.certificates, "passed": passed},
    )


async def recommendations_for(session: AsyncSession, user_id: uuid.UUID) -> RecommendationsRead:
    """Her next steps, and the courses and listings behind them."""
    ctx = await load_context(session, user_id)
    return RecommendationsRead(
        assessed=bool(ctx.scores),
        next_steps=next_steps(ctx),
        programs=program_suggestions(ctx),
        paths=path_suggestions(ctx),
        tasks=task_suggestions(ctx),
        opportunities=opportunity_suggestions(ctx),
    )


# --- skills she is missing --------------------------------------------------


@dataclass(slots=True)
class _Gap:
    ref: SkillRef
    dimension: ScoreDimension | None
    programs: int = 0
    opportunities: int = 0
    program: Program | None = None


def skill_gaps(ctx: LearnerContext, limit: int = MAX_SKILL_GAPS) -> list[SkillGapRead]:
    """Skills she does not hold that the live offer can actually give her.

    Counted from the catalogue and the open listings rather than from a wish
    list, so every gap named here has a course that teaches it or an employer
    asking for it. Skills tied to the dimensions that need her most come first,
    then whatever the most listings ask for.
    """
    focus = focus_dimensions(ctx)
    gaps: dict[str, _Gap] = {}

    def entry(label: str) -> _Gap | None:
        if ctx.holds(label):
            return None
        key = ctx.index.key(label)
        if key not in gaps:
            ref = ctx.index.ref(label)
            # The dimension it is shown under: one that needs her when the
            # skill touches it, otherwise whatever the skill is filed under.
            dimension = next((d for d in focus if d in ref.dimensions), None)
            gaps[key] = _Gap(ref=ref, dimension=dimension or next(iter(ref.dimensions), None))
        return gaps[key]

    for program in ctx.catalogue:
        for label in program.skills_taught:
            found = entry(label)
            if found is None:
                continue
            found.programs += 1
            if found.program is None:
                found.program = program

    for opportunity in ctx.opportunities:
        for label in opportunity.required_skills:
            found = entry(label)
            if found is not None:
                found.opportunities += 1

    ordered = sorted(
        gaps.values(),
        key=lambda gap: (
            0 if gap.dimension in focus else 1,
            -gap.opportunities,
            -gap.programs,
            gap.ref.label,
        ),
    )
    return [
        SkillGapRead(
            skill=gap.ref,
            dimension=gap.dimension,
            programs=gap.programs,
            opportunities=gap.opportunities,
            program_id=gap.program.id if gap.program else None,
            program_title_i18n=gap.program.title_i18n if gap.program else {},
        )
        for gap in ordered[:limit]
    ]


async def skills_to_improve(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = MAX_SKILL_GAPS
) -> list[SkillGapRead]:
    """The "skills to improve" half of her skill profile."""
    return skill_gaps(await load_context(session, user_id), limit)


# --- the score, explained ---------------------------------------------------


async def _answered_questions(
    session: AsyncSession, scores: dict[ScoreDimension, DevelopmentScore]
) -> dict[ScoreDimension, list[AnsweredQuestion]]:
    """Her answers from the assessment that last measured each dimension.

    Keyed per dimension because a later run may re-measure only some of them:
    a reading must be explained by the answers that produced the number on
    screen, not by an older run.
    """
    assessment_ids = {score.assessment_id for score in scores.values() if score.assessment_id}
    if not assessment_ids:
        return {}

    rows = await session.execute(
        select(
            AssessmentAnswer.assessment_id,
            AssessmentAnswer.question_id,
            AssessmentAnswer.value,
            AssessmentQuestion.dimension,
            AssessmentQuestion.text_i18n,
            AssessmentQuestion.options,
        )
        .join(AssessmentQuestion, AssessmentAnswer.question_id == AssessmentQuestion.id)
        .where(AssessmentAnswer.assessment_id.in_(assessment_ids))
        .order_by(AssessmentQuestion.order_index)
    )

    answered: dict[ScoreDimension, list[AnsweredQuestion]] = defaultdict(list)
    for assessment_id, question_id, value, dimension, text_i18n, options in rows:
        score = scores.get(dimension)
        if score is None or score.assessment_id != assessment_id:
            continue
        answered[dimension].append(AnsweredQuestion(question_id, text_i18n, options, value))
    return answered


def _answer_insight(answer: AnsweredQuestion) -> AnswerInsight:
    return AnswerInsight(
        question_id=answer.question_id,
        text_i18n=answer.text_i18n,
        answer_i18n=answer_label(answer.options, answer.value),
        value=answer.value,
    )


def _dimension_skill_gaps(
    ctx: LearnerContext, dimension: ScoreDimension, limit: int = 5
) -> list[SkillRef]:
    """Skills this dimension's own courses and listings need that she lacks.

    The link the cabinet reads as "you can improve this area by developing
    these skills" — drawn from what the platform offers for the dimension, so
    every one of them is closable here.
    """
    seen: set[str] = set()
    refs: list[SkillRef] = []

    def take(labels: Iterable[str]) -> None:
        for label in labels:
            if ctx.holds(label):
                continue
            key = ctx.index.key(label)
            if key in seen:
                continue
            seen.add(key)
            refs.append(ctx.index.ref(label))

    types = DIMENSION_OPPORTUNITY_TYPES[dimension]
    for opportunity in matching_opportunities(ctx):
        if opportunity.type in types:
            take(opportunity.required_skills)
    for program in programs_for_dimension(ctx, dimension)[:3]:
        take(program.skills_taught)
    return refs[:limit]


async def dimension_insights(session: AsyncSession, user_id: uuid.UUID) -> ScoreInsightsRead | None:
    """The score read dimension by dimension, or None before any assessment."""
    ctx = await load_context(session, user_id)
    if not ctx.scores:
        return None

    answered = await _answered_questions(session, ctx.scores)
    current = ctx.current
    dimensions: list[DimensionInsight] = []
    for dimension in ScoreDimension:
        score = ctx.scores.get(dimension)
        if score is None:
            continue
        strengths, weaknesses = split_answers(answered.get(dimension, []))
        dimensions.append(
            DimensionInsight(
                dimension=dimension,
                baseline=score.baseline,
                current=score.current,
                target=score.target,
                progress=score.progress,
                weight=SCORE_WEIGHTS[dimension],
                band=band_for(score.current),
                strengths=[_answer_insight(answer) for answer in strengths],
                weaknesses=[_answer_insight(answer) for answer in weaknesses],
                skill_gaps=_dimension_skill_gaps(ctx, dimension),
                actions=dimension_actions(ctx, dimension),
            )
        )

    return ScoreInsightsRead(
        composite=composite_score(current),
        dimensions=dimensions,
        measured_at=max(
            (s.measured_at for s in ctx.scores.values() if s.measured_at), default=None
        ),
        weakest_dimensions=weakest_dimensions(current),
        focus_dimensions=focus_dimensions(ctx),
    )
