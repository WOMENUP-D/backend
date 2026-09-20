"""The recommendation engine: what she is told to do next, and why.

These pin down the promises the cabinet makes out loud. Her own decisions come
first — a plan she confirmed, a course she started. Nothing finished, closed or
outside her age range is offered. A listing is only called a match when it
names skills she has. There are never more than three steps, and no course is
offered twice.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    DimensionBand,
    EnrollmentStatus,
    GoalHorizon,
    Language,
    NextStepKind,
    OpportunitySource,
    OpportunityType,
    PlanItemStatus,
    ProgramCategory,
    ProgramFormat,
    RecommendationReason,
    Region,
    ScoreDimension,
)
from app.models.assessment import Assessment, AssessmentAnswer, AssessmentQuestion
from app.models.opportunity import Opportunity
from app.models.plan import DevelopmentPlan, PlanItem
from app.models.profile import Profile
from app.models.program import Enrollment, Program
from app.models.user import User
from app.services.plan_service import generate_plan
from app.services.recommendation import (
    dimension_insights,
    load_context,
    next_steps,
    opportunity_suggestions,
    program_suggestions,
)
from app.services.scoring import calculate_dimension_scores, persist_scores

OPTIONS = [
    {"value": 0, "label_i18n": {"en": "Not at all"}},
    {"value": 25, "label_i18n": {"en": "Very little"}},
    {"value": 50, "label_i18n": {"en": "Moderate"}},
    {"value": 75, "label_i18n": {"en": "Good"}},
    {"value": 100, "label_i18n": {"en": "Excellent"}},
]


async def _user(session: AsyncSession, *, skills=(), age: int | None = 30) -> uuid.UUID:
    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", region=Region.TASHKENT_CITY)
    session.add(user)
    await session.flush()
    born = date(date.today().year - age, 1, 1) if age is not None else None
    session.add(Profile(user_id=user.id, skills=list(skills), birth_date=born))
    await session.flush()
    return user.id


async def _program(
    session: AsyncSession,
    category: ProgramCategory,
    *,
    skills=("test",),
    age_min: int | None = None,
    age_max: int | None = None,
) -> Program:
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"en": f"{category.value} course"},
        goal_i18n={},
        description_i18n={},
        category=category,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        learning_outcomes=[],
        skills_taught=list(skills),
        target_regions=[],
        target_age_min=age_min,
        target_age_max=age_max,
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(program)
    await session.flush()
    return program


async def _opportunity(
    session: AsyncSession,
    kind: OpportunityType,
    *,
    skills=(),
    days: int = 10,
    active: bool = True,
) -> Opportunity:
    opportunity = Opportunity(
        source=OpportunitySource.INTERNAL,
        external_id=uuid.uuid4().hex,
        type=kind,
        title_i18n={"en": kind.value},
        required_skills=list(skills),
        deadline=datetime.now(UTC) + timedelta(days=days),
        is_active=active,
    )
    session.add(opportunity)
    await session.flush()
    return opportunity


async def _score(
    session: AsyncSession, user_id: uuid.UUID, answers: dict[ScoreDimension, list[float]]
) -> None:
    """Score her through the real path: questions, answers, calculation, persistence."""
    now = datetime.now(UTC)
    assessment = Assessment(user_id=user_id, started_at=now, completed_at=now)
    session.add(assessment)
    await session.flush()
    for dimension, values in answers.items():
        for index, value in enumerate(values):
            question = AssessmentQuestion(
                dimension=dimension,
                order_index=index,
                text_i18n={"en": f"{dimension.value} question {index}"},
                options=OPTIONS,
            )
            session.add(question)
            await session.flush()
            session.add(
                AssessmentAnswer(assessment_id=assessment.id, question_id=question.id, value=value)
            )
    await session.flush()
    scores = await calculate_dimension_scores(session, assessment.id)
    await persist_scores(session, user_id, assessment.id, scores)
    await session.flush()


async def _enrol(
    session: AsyncSession,
    user_id: uuid.UUID,
    program: Program,
    status: EnrollmentStatus,
    progress: int = 0,
) -> None:
    session.add(
        Enrollment(
            user_id=user_id,
            program_id=program.id,
            status=status,
            progress_percent=progress,
            started_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def _steps(session: AsyncSession, user_id: uuid.UUID):
    return next_steps(await load_context(session, user_id))


@pytest.mark.asyncio
async def test_without_a_score_the_assessment_comes_first(session):
    user_id = await _user(session)

    steps = await _steps(session, user_id)

    assert steps[0].kind is NextStepKind.TAKE_ASSESSMENT
    assert await dimension_insights(session, user_id) is None


@pytest.mark.asyncio
async def test_the_weakest_dimension_supplies_the_new_course(session):
    user_id = await _user(session)
    await _score(
        session,
        user_id,
        {ScoreDimension.EDUCATION_SKILLS: [25.0], ScoreDimension.HEALTHY_LIFESTYLE: [100.0]},
    )
    health = await _program(session, ProgramCategory.HEALTH)
    vocational = await _program(session, ProgramCategory.VOCATIONAL_SKILLS)

    steps = await _steps(session, user_id)

    start = next(step for step in steps if step.kind is NextStepKind.START_PROGRAM)
    assert start.program.id == vocational.id
    assert start.dimension is ScoreDimension.EDUCATION_SKILLS
    assert start.reason is RecommendationReason.FOCUS_DIMENSION
    assert start.params == {"score": 25}
    # Her healthy-lifestyle score is strong, so nothing is pushed there.
    assert health.id not in {step.program.id for step in steps if step.program}


@pytest.mark.asyncio
async def test_a_finished_course_is_never_offered_and_a_started_one_leads(session):
    user_id = await _user(session)
    await _score(session, user_id, {ScoreDimension.EDUCATION_SKILLS: [25.0]})
    finished = await _program(session, ProgramCategory.VOCATIONAL_SKILLS)
    started = await _program(session, ProgramCategory.VOCATIONAL_SKILLS)
    fresh = await _program(session, ProgramCategory.VOCATIONAL_SKILLS)
    await _enrol(session, user_id, finished, EnrollmentStatus.COMPLETED, 100)
    await _enrol(session, user_id, started, EnrollmentStatus.IN_PROGRESS, 40)

    ctx = await load_context(session, user_id)
    steps = next_steps(ctx)

    assert [step.kind for step in steps[:2]] == [
        NextStepKind.CONTINUE_PROGRAM,
        NextStepKind.START_PROGRAM,
    ]
    assert steps[0].program.id == started.id
    assert steps[0].params == {"progress": 40}
    assert steps[1].program.id == fresh.id
    offered = {step.program.id for step in steps if step.program}
    offered |= {suggestion.id for suggestion in program_suggestions(ctx)}
    assert finished.id not in offered


@pytest.mark.asyncio
async def test_a_course_she_never_opened_is_not_described_as_progress(session):
    """ "0% done" beside a course she has not started reads as a failure, not a step."""
    user_id = await _user(session)
    course = await _program(session, ProgramCategory.VOCATIONAL_SKILLS)
    await _enrol(session, user_id, course, EnrollmentStatus.ENROLLED, 0)

    continued = next(
        step
        for step in await _steps(session, user_id)
        if step.kind is NextStepKind.CONTINUE_PROGRAM
    )

    assert continued.reason is RecommendationReason.ENROLLED


@pytest.mark.asyncio
async def test_the_plan_she_confirmed_comes_before_anything_the_engine_suggests(session):
    user_id = await _user(session)
    await _score(session, user_id, {ScoreDimension.EMPLOYMENT: [25.0]})
    await _program(session, ProgramCategory.VOCATIONAL_SKILLS)
    session.add(
        DevelopmentPlan(
            user_id=user_id,
            title="Plan",
            horizon=GoalHorizon.M6,
            is_active=True,
            accepted_at=datetime.now(UTC),
            items=[
                PlanItem(order_index=0, action="Skipped step", status=PlanItemStatus.SKIPPED),
                PlanItem(order_index=1, action="Done step", status=PlanItemStatus.DONE),
                PlanItem(order_index=2, action="Update my CV", status=PlanItemStatus.PLANNED),
            ],
        )
    )
    await session.flush()

    steps = await _steps(session, user_id)

    assert steps[0].kind is NextStepKind.PLAN_ITEM
    assert steps[0].text == "Update my CV"
    assert steps[0].reason is RecommendationReason.IN_YOUR_PLAN
    assert all(
        step.kind not in (NextStepKind.CREATE_PLAN, NextStepKind.REVIEW_PLAN) for step in steps
    )


@pytest.mark.asyncio
async def test_a_plan_is_suggested_only_while_she_has_none(session):
    user_id = await _user(session)
    await _score(session, user_id, {ScoreDimension.EMPLOYMENT: [25.0]})

    assert NextStepKind.CREATE_PLAN in {step.kind for step in await _steps(session, user_id)}

    # A proposal waiting for her is offered for review, not built again.
    session.add(DevelopmentPlan(user_id=user_id, title="Draft", is_active=False))
    await session.flush()

    kinds = {step.kind for step in await _steps(session, user_id)}
    assert NextStepKind.REVIEW_PLAN in kinds
    assert NextStepKind.CREATE_PLAN not in kinds


@pytest.mark.asyncio
async def test_never_more_than_three_steps_and_no_course_twice(session):
    user_id = await _user(session, skills=["excel"])
    await _score(
        session,
        user_id,
        {ScoreDimension.EMPLOYMENT: [25.0], ScoreDimension.EDUCATION_SKILLS: [25.0]},
    )
    course = await _program(session, ProgramCategory.VOCATIONAL_SKILLS)
    await _program(session, ProgramCategory.VOCATIONAL_SKILLS)
    await _enrol(session, user_id, course, EnrollmentStatus.IN_PROGRESS, 20)
    session.add(
        DevelopmentPlan(
            user_id=user_id,
            title="Plan",
            is_active=True,
            items=[PlanItem(order_index=0, action="Finish the course", program_id=course.id)],
        )
    )
    await _opportunity(session, OpportunityType.VACANCY, skills=["Excel"])
    await session.flush()

    steps = await _steps(session, user_id)

    assert len(steps) == 3
    offered = [step.program.id for step in steps if step.program]
    assert len(offered) == len(set(offered))


@pytest.mark.asyncio
async def test_only_listings_that_name_her_skills_are_called_a_match(session):
    user_id = await _user(session, skills=["excel"])
    await _score(session, user_id, {ScoreDimension.ENTREPRENEURSHIP: [25.0]})
    grant = await _opportunity(session, OpportunityType.GRANT)
    vacancy = await _opportunity(session, OpportunityType.VACANCY, skills=["Excel", "1C"])

    suggestions = opportunity_suggestions(await load_context(session, user_id))

    assert suggestions[0].id == vacancy.id
    assert suggestions[0].reason is RecommendationReason.SKILLS_MATCH
    assert suggestions[0].match == 0.5
    # Skills travel as references now; `label` is the word the listing used.
    assert [ref.label for ref in suggestions[0].matched_skills] == ["Excel"]
    grant_card = next(s for s in suggestions if s.id == grant.id)
    assert grant_card.match is None
    assert grant_card.reason is RecommendationReason.FOCUS_DIMENSION
    assert grant_card.dimension is ScoreDimension.ENTREPRENEURSHIP


@pytest.mark.asyncio
async def test_closed_listings_are_never_suggested(session):
    user_id = await _user(session, skills=["excel"])
    expired = await _opportunity(session, OpportunityType.VACANCY, skills=["excel"], days=-1)
    inactive = await _opportunity(session, OpportunityType.VACANCY, skills=["excel"], active=False)

    ctx = await load_context(session, user_id)

    assert not {expired.id, inactive.id} & {s.id for s in opportunity_suggestions(ctx)}
    assert all(step.kind is not NextStepKind.EXPLORE_OPPORTUNITIES for step in next_steps(ctx))


@pytest.mark.asyncio
async def test_a_course_outside_her_age_is_not_offered(session):
    user_id = await _user(session, age=15)
    await _score(session, user_id, {ScoreDimension.EDUCATION_SKILLS: [25.0]})
    adults = await _program(session, ProgramCategory.VOCATIONAL_SKILLS, age_min=18)
    teens = await _program(session, ProgramCategory.VOCATIONAL_SKILLS, age_min=13, age_max=17)

    ctx = await load_context(session, user_id)
    offered = {s.id for s in program_suggestions(ctx)}
    offered |= {step.program.id for step in next_steps(ctx) if step.program}

    assert teens.id in offered
    assert adults.id not in offered


@pytest.mark.asyncio
async def test_insights_explain_a_dimension_with_her_own_answers(session):
    user_id = await _user(session, skills=["Excel"])
    await _score(session, user_id, {ScoreDimension.EMPLOYMENT: [100.0, 50.0, 0.0]})
    await _program(session, ProgramCategory.VOCATIONAL_SKILLS, skills=["excel", "1C"])

    insights = await dimension_insights(session, user_id)

    assert insights is not None
    assert insights.composite == 50.0
    [employment] = insights.dimensions
    assert employment.dimension is ScoreDimension.EMPLOYMENT
    assert employment.band is DimensionBand.DEVELOPING
    assert [a.value for a in employment.strengths] == [100.0]
    assert employment.strengths[0].answer_i18n == {"en": "Excellent"}
    assert [a.value for a in employment.weaknesses] == [0.0]
    # "excel" is already hers, whatever the case; only what she lacks is a gap.
    assert [ref.label for ref in employment.skill_gaps] == ["1C"]
    assert NextStepKind.START_PROGRAM in {action.kind for action in employment.actions}
    assert insights.focus_dimensions == [ScoreDimension.EMPLOYMENT]


@pytest.mark.asyncio
async def test_rule_based_plan_finds_a_course_for_education_and_skills(session):
    """The fallback looked for the dimension's name inside the category's.

    `education_skills` is not inside `vocational_skills`, so six dimensions out
    of eight got a "choose a programme" placeholder even with a course on offer.
    """
    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}")
    session.add(user)
    await session.flush()
    course = await _program(session, ProgramCategory.VOCATIONAL_SKILLS)

    plan = await generate_plan(
        session, user_id=user.id, focus_dimensions=[ScoreDimension.EDUCATION_SKILLS]
    )

    assert plan.generated_by_ai is False
    assert plan.items[0].program_id == course.id
