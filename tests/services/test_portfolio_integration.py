"""The portfolio inside the AI Coach and the recommendation engine.

The Coach may name a certificate, a project or an achievement only if it is in
her record — so these check what reaches the prompt, not what the model says.
And the engine's new step must be invisible to a woman with nothing to show,
which is what keeps every earlier step's ordering intact.
"""

import uuid
from datetime import UTC, date, datetime

import pytest

from app.core.constants import (
    ConsentScope,
    EvaluatorKind,
    Language,
    NextStepKind,
    ProgramCategory,
    ProgramFormat,
    RecommendationReason,
    Region,
    ScoreDimension,
    TaskStatus,
    TaskSubmissionKind,
)
from app.models.assessment import Assessment, AssessmentAnswer, AssessmentQuestion
from app.models.consent import ConsentLog
from app.models.portfolio import PortfolioProject
from app.models.practice import PracticalTask, TaskAttempt
from app.models.profile import Profile
from app.models.program import Certificate, Enrollment, Program
from app.models.user import User
from app.services import ai_coach
from app.services.recommendation import load_context, next_steps
from app.services.scoring import calculate_dimension_scores, persist_scores

OPTIONS = [
    {"value": 0, "label_i18n": {"en": "No"}},
    {"value": 100, "label_i18n": {"en": "Yes"}},
]


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _woman(session, user_id: uuid.UUID) -> None:
    session.add(User(id=user_id, phone=_phone(), region=Region.TASHKENT_CITY, language=Language.UZ))
    session.add(Profile(user_id=user_id, skills=[], birth_date=date(1990, 1, 1)))
    session.add(
        ConsentLog(
            user_id=user_id,
            scope=ConsentScope.AI_PERSONALISATION,
            accepted=True,
            policy_version="1.0",
            accepted_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def _certificate(session, user_id: uuid.UUID, title: str) -> Certificate:
    program = Program(
        slug=f"c-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": title, "en": title},
        goal_i18n={},
        description_i18n={},
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        learning_outcomes=[],
        skills_taught=[],
        has_certificate=True,
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(program)
    await session.flush()
    enrollment = Enrollment(user_id=user_id, program_id=program.id, progress_percent=100)
    session.add(enrollment)
    await session.flush()
    certificate = Certificate(
        enrollment_id=enrollment.id,
        user_id=user_id,
        serial_number=f"WU-2026-{uuid.uuid4().hex[:8].upper()}",
        issued_at=datetime.now(UTC),
        verification_code=uuid.uuid4().hex,
    )
    session.add(certificate)
    await session.flush()
    return certificate


async def _passed_task(session, user_id: uuid.UUID) -> None:
    task = PracticalTask(
        slug=f"t-{uuid.uuid4().hex[:6]}",
        title_i18n={"uz": "Topshiriq"},
        summary_i18n={},
        instructions_i18n={},
        outcome_i18n={},
        criteria=[],
        kind=TaskSubmissionKind.TEXT,
        skills_practised=[],
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(task)
    await session.flush()
    session.add(
        TaskAttempt(
            task_id=task.id,
            user_id=user_id,
            attempt_no=1,
            status=TaskStatus.PASSED,
            passed=True,
            evaluator_kind=EvaluatorKind.TRAINER,
            evaluated_at=datetime.now(UTC),
            submitted_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def _score(session, user_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    assessment = Assessment(user_id=user_id, started_at=now, completed_at=now)
    session.add(assessment)
    await session.flush()
    question = AssessmentQuestion(
        dimension=ScoreDimension.EMPLOYMENT, order_index=0, text_i18n={"en": "q"}, options=OPTIONS
    )
    session.add(question)
    await session.flush()
    session.add(AssessmentAnswer(assessment_id=assessment.id, question_id=question.id, value=0))
    await session.flush()
    await persist_scores(
        session, user_id, assessment.id, await calculate_dimension_scores(session, assessment.id)
    )
    await session.flush()


# --- the Coach --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_coach_is_told_the_certificates_and_projects_that_exist(session, user_id):
    await _woman(session, user_id)
    cert = await _certificate(session, user_id, "Buxgalteriya asoslari")
    session.add(PortfolioProject(user_id=user_id, title="Doʻkon hisobi jadvali"))
    await session.flush()

    context = await ai_coach.build(session, user_id)
    prompt = context.as_prompt()

    assert "Buxgalteriya asoslari" in prompt
    assert cert.serial_number in prompt
    assert "Doʻkon hisobi jadvali" in prompt
    # Her projects are labelled as her claims, not as verification.
    assert "her own claims, not verified" in prompt
    assert context.read.portfolio.projects == ["Doʻkon hisobi jadvali"]


@pytest.mark.asyncio
async def test_with_nothing_to_show_the_coach_is_told_so(session, user_id):
    await _woman(session, user_id)

    prompt = (await ai_coach.build(session, user_id)).as_prompt()

    assert "certificates she holds: none yet" in prompt
    assert "projects in her portfolio: none yet" in prompt
    assert "achievements WomanUP can back with a record: 0" in prompt


@pytest.mark.asyncio
async def test_another_womans_certificate_never_reaches_her_coach(session, user_id):
    await _woman(session, user_id)
    other = uuid.uuid4()
    await _woman(session, other)
    theirs = await _certificate(session, other, "Boshqa ayolning kursi")

    prompt = (await ai_coach.build(session, user_id)).as_prompt()

    assert "Boshqa ayolning kursi" not in prompt
    assert theirs.serial_number not in prompt


@pytest.mark.asyncio
async def test_the_portfolio_question_is_offered_only_when_there_is_something_to_show(
    session, user_id
):
    await _woman(session, user_id)
    empty = (await ai_coach.build(session, user_id)).read.suggestions
    assert "coach.q.portfolio" not in [item.key for item in empty]

    await _certificate(session, user_id, "Kurs")
    offered = (await ai_coach.build(session, user_id)).read.suggestions
    assert "coach.q.portfolio" in [item.key for item in offered]

    session.add(PortfolioProject(user_id=user_id, title="Loyiha"))
    await session.flush()
    written = (await ai_coach.build(session, user_id)).read.suggestions
    assert "coach.q.portfolio" not in [item.key for item in written]


@pytest.mark.asyncio
async def test_the_coach_prompt_forbids_inventing_portfolio_records():
    from app.services.prompts import COACH_SYSTEM

    assert "certificate, project or" in COACH_SYSTEM
    assert "achievement names" in COACH_SYSTEM
    assert "never mention a certificate, project or" in COACH_SYSTEM


# --- the engine -------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_nothing_to_show_the_steps_are_unchanged(session, user_id):
    """The write-up step is invisible to a woman with nothing done yet."""
    await _woman(session, user_id)
    await _score(session, user_id)

    steps = next_steps(await load_context(session, user_id))

    assert NextStepKind.ADD_PROJECT not in [step.kind for step in steps]


@pytest.mark.asyncio
async def test_passed_work_with_no_project_suggests_writing_it_up(session, user_id):
    await _woman(session, user_id)
    await _passed_task(session, user_id)

    steps = next_steps(await load_context(session, user_id))

    [step] = [s for s in steps if s.kind == NextStepKind.ADD_PROJECT]
    assert step.reason == RecommendationReason.EVIDENCE_TO_SHOW
    assert step.params["passed"] == 1


@pytest.mark.asyncio
async def test_a_certificate_alone_is_enough_to_suggest_it(session, user_id):
    await _woman(session, user_id)
    await _certificate(session, user_id, "Kurs")

    steps = next_steps(await load_context(session, user_id))

    assert NextStepKind.ADD_PROJECT in [step.kind for step in steps]


@pytest.mark.asyncio
async def test_once_she_has_a_project_it_is_not_suggested_again(session, user_id):
    await _woman(session, user_id)
    await _passed_task(session, user_id)
    session.add(PortfolioProject(user_id=user_id, title="Loyiha"))
    await session.flush()

    steps = next_steps(await load_context(session, user_id))

    assert NextStepKind.ADD_PROJECT not in [step.kind for step in steps]


@pytest.mark.asyncio
async def test_writing_up_work_never_displaces_learning(session, user_id):
    """Last in the order: with a full list ahead of it, it simply does not show."""
    await _woman(session, user_id)
    await _passed_task(session, user_id)

    steps = next_steps(await load_context(session, user_id))

    # No score yet, so the assessment still leads, as it always has.
    assert steps[0].kind == NextStepKind.TAKE_ASSESSMENT
    assert steps[-1].kind == NextStepKind.ADD_PROJECT
