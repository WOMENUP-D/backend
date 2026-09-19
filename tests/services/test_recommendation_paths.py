"""Learning paths inside the recommendation engine.

The engine gained one rule: between the course she is on and a new one, offer a
route. These pin what that rule may and may not do — above all that it changes
nothing for a platform with no paths in it, which is what "do not silently
change the existing behaviour" has to mean in tests rather than in prose.
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    EnrollmentStatus,
    Language,
    NextStepKind,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
    RecommendationReason,
    Region,
    ScoreDimension,
)
from app.models.assessment import Assessment, AssessmentAnswer, AssessmentQuestion
from app.models.learning_path import LearningPath, LearningPathItem, UserLearningPath
from app.models.profile import Profile
from app.models.program import Enrollment, Program
from app.models.user import User
from app.services.recommendation import (
    dimension_actions,
    load_context,
    next_steps,
    path_suggestions,
)
from app.services.scoring import calculate_dimension_scores, persist_scores

OPTIONS = [
    {"value": 0, "label_i18n": {"en": "Not at all"}},
    {"value": 100, "label_i18n": {"en": "Excellent"}},
]


async def _user(session: AsyncSession) -> uuid.UUID:
    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", region=Region.TASHKENT_CITY)
    session.add(user)
    await session.flush()
    session.add(Profile(user_id=user.id, skills=[], birth_date=date(date.today().year - 30, 1, 1)))
    await session.flush()
    return user.id


async def _program(
    session: AsyncSession, category: ProgramCategory, *, skills=("test",)
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
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(program)
    await session.flush()
    return program


async def _path(
    session: AsyncSession,
    programs: list[Program],
    *,
    dimension: ScoreDimension,
    slug: str | None = None,
) -> LearningPath:
    path = LearningPath(
        slug=slug or f"path-{uuid.uuid4().hex[:8]}",
        title_i18n={"en": "Route"},
        description_i18n={},
        dimension=dimension,
        level=ProficiencyLevel.BEGINNER,
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(path)
    await session.flush()
    for index, program in enumerate(programs):
        session.add(LearningPathItem(path_id=path.id, program_id=program.id, order_index=index))
    await session.flush()
    await session.refresh(path, ["items"])
    return path


async def _score(
    session: AsyncSession, user_id: uuid.UUID, answers: dict[ScoreDimension, list[float]]
) -> None:
    now = datetime.now(UTC)
    assessment = Assessment(user_id=user_id, started_at=now, completed_at=now)
    session.add(assessment)
    await session.flush()
    for dimension, values in answers.items():
        for index, value in enumerate(values):
            question = AssessmentQuestion(
                dimension=dimension,
                order_index=index,
                text_i18n={"en": f"{dimension.value} {index}"},
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


# --- compatibility ----------------------------------------------------------


@pytest.mark.asyncio
async def test_with_no_paths_in_the_catalogue_nothing_about_the_steps_changes(session):
    """The rule is a no-op on a platform that has no paths. This is what keeps
    Step 2's ordering intact for everyone who has not seeded routes."""
    user_id = await _user(session)
    await _program(session, ProgramCategory.FINANCIAL_LITERACY)
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: [0.0]})

    ctx = await load_context(session, user_id)
    steps = next_steps(ctx)

    assert [step.kind for step in steps] == [
        NextStepKind.START_PROGRAM,
        NextStepKind.CREATE_PLAN,
    ]
    assert path_suggestions(ctx) == []


# --- what a path earns ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_route_is_offered_for_the_dimension_that_needs_her_most(session):
    user_id = await _user(session)
    program = await _program(session, ProgramCategory.FINANCIAL_LITERACY)
    path = await _path(session, [program], dimension=ScoreDimension.FINANCIAL_LITERACY)
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: [0.0]})

    steps = next_steps(await load_context(session, user_id))

    [step] = [item for item in steps if item.kind == NextStepKind.START_PATH]
    assert step.reason == RecommendationReason.FOCUS_DIMENSION
    assert step.dimension == ScoreDimension.FINANCIAL_LITERACY
    assert step.path is not None
    assert step.path.slug == path.slug
    assert step.path.program_count == 1
    assert step.path.started is False
    # The route says what it would teach that she does not hold.
    assert [ref.label for ref in step.path.new_skills] == ["test"]


@pytest.mark.asyncio
async def test_a_route_she_is_part_way_along_comes_before_a_new_one(session):
    user_id = await _user(session)
    started_course = await _program(session, ProgramCategory.FINANCIAL_LITERACY)
    other_course = await _program(session, ProgramCategory.FINANCIAL_LITERACY)
    walking = await _path(
        session,
        [started_course, other_course],
        dimension=ScoreDimension.FINANCIAL_LITERACY,
        slug="walking",
    )
    await _path(
        session,
        [await _program(session, ProgramCategory.FINANCIAL_LITERACY)],
        dimension=ScoreDimension.FINANCIAL_LITERACY,
        slug="untouched",
    )
    await _enrol(session, user_id, started_course, EnrollmentStatus.COMPLETED, 100)
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: [0.0]})

    steps = next_steps(await load_context(session, user_id))

    [step] = [item for item in steps if item.kind == NextStepKind.CONTINUE_PATH]
    assert step.path is not None
    assert step.path.slug == walking.slug
    assert step.reason == RecommendationReason.IN_PROGRESS
    assert step.path.percent == 50
    assert step.path.completed_count == 1


@pytest.mark.asyncio
async def test_a_finished_route_is_never_offered_again(session):
    user_id = await _user(session)
    program = await _program(session, ProgramCategory.FINANCIAL_LITERACY)
    path = await _path(session, [program], dimension=ScoreDimension.FINANCIAL_LITERACY)
    session.add(UserLearningPath(user_id=user_id, path_id=path.id, started_at=datetime.now(UTC)))
    await _enrol(session, user_id, program, EnrollmentStatus.COMPLETED, 100)
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: [0.0]})

    ctx = await load_context(session, user_id)

    assert [step.kind for step in next_steps(ctx) if step.path is not None] == []
    assert path_suggestions(ctx) == []


@pytest.mark.asyncio
async def test_a_step_she_confirmed_still_leads(session):
    """The path rule sits below her own decisions, not above them: the course
    she has open is still what she is told to finish first."""
    user_id = await _user(session)
    open_course = await _program(session, ProgramCategory.FINANCIAL_LITERACY)
    await _path(
        session,
        [await _program(session, ProgramCategory.FINANCIAL_LITERACY)],
        dimension=ScoreDimension.FINANCIAL_LITERACY,
    )
    await _enrol(session, user_id, open_course, EnrollmentStatus.IN_PROGRESS, 40)
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: [0.0]})

    steps = next_steps(await load_context(session, user_id))

    assert steps[0].kind == NextStepKind.CONTINUE_PROGRAM
    assert steps[0].program is not None
    assert steps[0].program.id == open_course.id


@pytest.mark.asyncio
async def test_a_route_leads_the_actions_for_the_dimension_it_builds(session):
    user_id = await _user(session)
    program = await _program(session, ProgramCategory.FINANCIAL_LITERACY)
    path = await _path(session, [program], dimension=ScoreDimension.FINANCIAL_LITERACY)
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: [0.0]})

    ctx = await load_context(session, user_id)
    actions = dimension_actions(ctx, ScoreDimension.FINANCIAL_LITERACY)

    assert actions[0].kind == NextStepKind.START_PATH
    assert actions[0].path is not None
    assert actions[0].path.slug == path.slug
    # And the single-course advice it always gave is still there underneath.
    assert NextStepKind.START_PROGRAM in [action.kind for action in actions]


@pytest.mark.asyncio
async def test_a_route_for_a_strong_dimension_is_not_pushed_at_her(session):
    user_id = await _user(session)
    program = await _program(session, ProgramCategory.FINANCIAL_LITERACY)
    await _path(session, [program], dimension=ScoreDimension.FINANCIAL_LITERACY)
    await _score(
        session,
        user_id,
        {
            ScoreDimension.FINANCIAL_LITERACY: [100.0],
            ScoreDimension.EMPLOYMENT: [0.0],
        },
    )

    ctx = await load_context(session, user_id)

    assert [step.kind for step in next_steps(ctx) if step.path is not None] == []
    assert path_suggestions(ctx) == []


@pytest.mark.asyncio
async def test_without_a_score_no_route_is_recommended(session):
    """Nothing is personal before the diagnostic, and an unranked route is just
    the catalogue with a progress bar on it."""
    user_id = await _user(session)
    program = await _program(session, ProgramCategory.FINANCIAL_LITERACY)
    await _path(session, [program], dimension=ScoreDimension.FINANCIAL_LITERACY)

    ctx = await load_context(session, user_id)

    assert [step.kind for step in next_steps(ctx)] == [NextStepKind.TAKE_ASSESSMENT]
    assert path_suggestions(ctx) == []


@pytest.mark.asyncio
async def test_an_enrollment_alone_puts_her_on_the_route(session):
    """She found one course in the catalogue. The progress is real, so the
    route it belongs to is real — and it is not claimed she started it."""
    user_id = await _user(session)
    first = await _program(session, ProgramCategory.FINANCIAL_LITERACY)
    second = await _program(session, ProgramCategory.FINANCIAL_LITERACY)
    await _path(session, [first, second], dimension=ScoreDimension.FINANCIAL_LITERACY)
    await _enrol(session, user_id, first, EnrollmentStatus.COMPLETED, 100)
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: [0.0]})

    [suggestion] = path_suggestions(await load_context(session, user_id))

    assert suggestion.percent == 50
    assert suggestion.started is False
