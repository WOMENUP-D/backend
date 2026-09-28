"""Practical tasks inside the Coach and the recommendation engine.

Two rules are being pinned. The engine gained a practice step and must still
behave exactly as it did for a platform that has no tasks in it — that is what
"integrate without breaking the existing rules" has to mean in a test rather
than in prose. And the Coach must see real practice state, quote real evaluator
feedback, and be unable to name a task the database does not hold.
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ConsentScope,
    EvidenceKind,
    Language,
    NextStepKind,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
    RecommendationReason,
    Region,
    ScoreDimension,
    SkillCategory,
    TaskStatus,
    TaskSubmissionKind,
)
from app.models.assessment import Assessment, AssessmentAnswer, AssessmentQuestion
from app.models.consent import ConsentLog
from app.models.practice import PracticalTask, TaskAttempt
from app.models.profile import Profile
from app.models.program import Program
from app.models.skill import Skill
from app.models.user import User
from app.services import ai_coach, skills
from app.services.recommendation import load_context, next_steps, task_suggestions
from app.services.scoring import calculate_dimension_scores, persist_scores

OPTIONS = [
    {"value": 0, "label_i18n": {"en": "Not at all"}},
    {"value": 100, "label_i18n": {"en": "Excellent"}},
]


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _woman(session: AsyncSession, user_id: uuid.UUID) -> None:
    session.add(User(id=user_id, phone=_phone(), region=Region.TASHKENT_CITY, language=Language.UZ))
    session.add(Profile(user_id=user_id, skills=[], birth_date=date(date.today().year - 30, 1, 1)))
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


async def _skill(session, slug: str, label: str) -> Skill:
    skill = Skill(
        slug=slug,
        name_i18n={"uz": label, "ru": label, "en": label},
        category=SkillCategory.DIGITAL,
        dimensions=[],
        aliases=[slug, label.lower()],
        is_curated=True,
    )
    session.add(skill)
    await session.flush()
    return skill


async def _course(session, *, taught: list[str]) -> Program:
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Kurs", "en": "Course"},
        goal_i18n={},
        description_i18n={},
        category=ProgramCategory.FINANCIAL_LITERACY,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        learning_outcomes=[],
        skills_taught=taught,
        target_regions=[],
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(program)
    await session.flush()
    return program


async def _task(
    session, *, slug: str, practises: list[str], published: bool = True
) -> PracticalTask:
    task = PracticalTask(
        slug=slug,
        title_i18n={"uz": f"Topshiriq {slug}", "en": f"Task {slug}"},
        summary_i18n={},
        instructions_i18n={"uz": "Koʻrsatma"},
        outcome_i18n={},
        criteria=[{"key": "clear", "text_i18n": {"uz": "Aniq"}}],
        kind=TaskSubmissionKind.TEXT,
        min_chars=20,
        level=ProficiencyLevel.ELEMENTARY,
        estimated_minutes=30,
        skills_practised=practises,
        is_published=published,
        published_at=datetime.now(UTC),
    )
    session.add(task)
    await session.flush()
    return task


async def _score(session, user_id: uuid.UUID, answers: dict[ScoreDimension, float]) -> None:
    now = datetime.now(UTC)
    assessment = Assessment(user_id=user_id, started_at=now, completed_at=now)
    session.add(assessment)
    await session.flush()
    for dimension, value in answers.items():
        question = AssessmentQuestion(
            dimension=dimension, order_index=0, text_i18n={"en": "q"}, options=OPTIONS
        )
        session.add(question)
        await session.flush()
        session.add(
            AssessmentAnswer(assessment_id=assessment.id, question_id=question.id, value=value)
        )
    await session.flush()
    await persist_scores(
        session, user_id, assessment.id, await calculate_dimension_scores(session, assessment.id)
    )
    await session.flush()


# --- the engine -------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_no_tasks_in_the_catalogue_the_steps_are_unchanged(session, user_id):
    """The practice rule is a no-op on a platform with no tasks. This is what
    keeps every earlier step's ordering intact for anyone who has not seeded
    them."""
    await _woman(session, user_id)
    await _course(session, taught=["Excel"])
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})

    ctx = await load_context(session, user_id)

    assert [step.kind for step in next_steps(ctx)] == [
        NextStepKind.START_PROGRAM,
        NextStepKind.CREATE_PLAN,
    ]
    assert task_suggestions(ctx) == []


@pytest.mark.asyncio
async def test_practice_is_offered_for_a_skill_she_already_holds(session, user_id):
    """Practice is what you do with something you were taught, so a task is
    offered on the strength of what she holds rather than what she lacks."""
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")
    await skills.record_course_completion(
        session, user_id=user_id, labels=["Excel"], enrollment_id=uuid.uuid4()
    )
    task = await _task(session, slug="excel-ish", practises=["Excel"])
    await _task(session, slug="hech-kim", practises=["Tikuvchilik"])
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})

    ctx = await load_context(session, user_id)
    steps = next_steps(ctx)

    [step] = [s for s in steps if s.kind == NextStepKind.PRACTISE_TASK]
    assert step.task is not None
    assert step.task.slug == task.slug
    assert step.task.status is None
    assert [ref.label for ref in step.task.skills] == ["Excel"]
    # The task practising something she has never been taught is not offered.
    assert "hech-kim" not in [row.slug for row in task_suggestions(ctx)]


@pytest.mark.asyncio
async def test_work_she_was_told_how_to_fix_comes_first(session, user_id):
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")
    await skills.record_course_completion(
        session, user_id=user_id, labels=["Excel"], enrollment_id=uuid.uuid4()
    )
    fresh = await _task(session, slug="yangi", practises=["Excel"])
    failed = await _task(session, slug="qayta", practises=["Excel"])
    session.add(
        TaskAttempt(
            task_id=failed.id,
            user_id=user_id,
            attempt_no=1,
            status=TaskStatus.NEEDS_IMPROVEMENT,
            submission={"text": "urinish"},
            passed=False,
            feedback="Jamgʻarma summasi yoʻq.",
            evaluated_at=datetime.now(UTC),
            submitted_at=datetime.now(UTC),
        )
    )
    await session.flush()
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})

    ctx = await load_context(session, user_id)
    steps = next_steps(ctx)

    [step] = [s for s in steps if s.task is not None]
    assert step.kind == NextStepKind.IMPROVE_TASK
    assert step.reason == RecommendationReason.NEEDS_IMPROVEMENT
    assert step.task.slug == failed.slug
    assert step.task.status == "needs_improvement"
    assert fresh.slug != step.task.slug


@pytest.mark.asyncio
async def test_a_passed_task_is_never_offered_again(session, user_id):
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")
    await skills.record_course_completion(
        session, user_id=user_id, labels=["Excel"], enrollment_id=uuid.uuid4()
    )
    task = await _task(session, slug="tugagan", practises=["Excel"])
    session.add(
        TaskAttempt(
            task_id=task.id,
            user_id=user_id,
            attempt_no=1,
            status=TaskStatus.PASSED,
            submission={"text": "ish"},
            passed=True,
            evaluated_at=datetime.now(UTC),
            submitted_at=datetime.now(UTC),
        )
    )
    await session.flush()
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})

    ctx = await load_context(session, user_id)

    assert [s for s in next_steps(ctx) if s.task is not None] == []
    assert task_suggestions(ctx) == []


@pytest.mark.asyncio
async def test_an_unpublished_task_never_reaches_the_engine(session, user_id):
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")
    await skills.record_course_completion(
        session, user_id=user_id, labels=["Excel"], enrollment_id=uuid.uuid4()
    )
    await _task(session, slug="qoralama", practises=["Excel"], published=False)
    await _score(session, user_id, {ScoreDimension.FINANCIAL_LITERACY: 0.0})

    ctx = await load_context(session, user_id)

    assert ctx.tasks == []
    assert task_suggestions(ctx) == []


# --- the Coach --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_coach_sees_real_practice_state(session, user_id):
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")
    passed = await _task(session, slug="tugagan", practises=["Excel"])
    failed = await _task(session, slug="qayta", practises=["Excel"])
    now = datetime.now(UTC)
    session.add_all(
        [
            TaskAttempt(
                task_id=passed.id,
                user_id=user_id,
                attempt_no=1,
                status=TaskStatus.PASSED,
                submission={"text": "ish"},
                passed=True,
                evaluated_at=now,
                submitted_at=now,
            ),
            TaskAttempt(
                task_id=failed.id,
                user_id=user_id,
                attempt_no=1,
                status=TaskStatus.NEEDS_IMPROVEMENT,
                submission={"text": "ish"},
                passed=False,
                feedback="Jamgʻarma summasi koʻrsatilmagan.",
                evaluated_at=now,
                submitted_at=now,
            ),
        ]
    )
    await session.flush()

    context = await ai_coach.build(session, user_id)
    practice = context.read.practice

    assert practice.passed == 1
    assert practice.needs_improvement == 1
    assert practice.next_task_slug == failed.slug
    assert practice.last_feedback == "Jamgʻarma summasi koʻrsatilmagan."

    prompt = context.as_prompt()
    assert "1 passed, 1 need another go" in prompt
    # The evaluator's own words, quoted rather than paraphrased.
    assert "Jamgʻarma summasi koʻrsatilmagan." in prompt
    assert "quoted exactly" in prompt


@pytest.mark.asyncio
async def test_the_coach_may_only_name_tasks_that_exist(session, user_id):
    """The grounding boundary, for tasks: an id the model invented is dropped
    before it can reach the browser."""
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")
    task = await _task(session, slug="haqiqiy", practises=["Excel"])
    await _course(session, taught=["Excel"])
    await _score(session, user_id, {ScoreDimension.EDUCATION_SKILLS: 0.0})

    context = await ai_coach.build(session, user_id)
    # The real task is nameable...
    assert str(task.id) in context.offer
    assert context.offer[str(task.id)].kind == "practical_task"
    assert "practical task; practises Excel" in context.as_prompt()

    class _Gateway:
        enabled = True

        @staticmethod
        def sanitise(text: str) -> str:
            return text

        async def complete(self, **kwargs):
            from app.services.llm_gateway import LlmResponse

            return LlmResponse(
                text="",
                parsed={
                    "message": "Try the Advanced Excel Mastery Challenge.",
                    "next_step_id": str(uuid.uuid4()),
                    "reference_ids": [str(uuid.uuid4()), str(task.id)],
                    "unsupported": False,
                },
                model="test",
                prompt_version="test",
                trace_id=uuid.uuid4().hex,
                latency_ms=1,
                input_tokens=1,
                output_tokens=1,
                refused=False,
            )

    reply = await ai_coach.answer(
        session, user_id=user_id, question="Qanday mashq qilay?", gateway=_Gateway()
    )

    # ...and the two it made up are gone.
    assert reply.next_step is None
    assert [ref.id for ref in reply.references] == [task.id]


@pytest.mark.asyncio
async def test_with_no_task_for_her_gaps_the_coach_is_told_to_say_so(session, user_id):
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")
    await _course(session, taught=["Excel"])
    await _score(session, user_id, {ScoreDimension.EDUCATION_SKILLS: 0.0})

    prompt = (await ai_coach.build(session, user_id)).as_prompt()

    assert "practical tasks: she has not attempted any yet" in prompt
    assert "none published right now" in prompt
    assert "rather than describing a task that does not exist" in prompt


@pytest.mark.asyncio
async def test_the_coach_offers_the_failed_task_as_a_question(session, user_id):
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")
    failed = await _task(session, slug="qayta", practises=["Excel"])
    session.add(
        TaskAttempt(
            task_id=failed.id,
            user_id=user_id,
            attempt_no=1,
            status=TaskStatus.NEEDS_IMPROVEMENT,
            submission={"text": "ish"},
            passed=False,
            feedback="Raqamlar yoʻq.",
            evaluated_at=datetime.now(UTC),
            submitted_at=datetime.now(UTC),
        )
    )
    await session.flush()

    read = (await ai_coach.build(session, user_id)).read

    question = next(item for item in read.suggestions if item.key == "coach.q.taskFailed")
    assert question.params["task"] == failed.title_i18n["uz"]


@pytest.mark.asyncio
async def test_the_coach_prompt_still_forbids_calling_a_passed_task_verified(session, user_id):
    await _woman(session, user_id)
    skill = await _skill(session, "excel", "Excel")
    task = await _task(session, slug="ish", practises=["Excel"])
    await skills.record_evidence(
        session,
        user_id=user_id,
        skill=skill,
        kind=EvidenceKind.FORMAL_ASSESSMENT,
        source_type="task_attempt",
        source_id=str(uuid.uuid4()),
        level=ProficiencyLevel.ELEMENTARY,
    )
    await session.flush()

    context = await ai_coach.build(session, user_id)

    # The pass made it assessed, and the context says exactly that.
    assert [row.skill.slug for row in context.read.skills.assessed] == ["excel"]
    assert context.read.skills.verified == []
    assert "assessed (an assessment scored)" in context.as_prompt()
    assert task.slug is not None
