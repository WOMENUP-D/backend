"""Practical tasks: doing the work, being assessed, and what that proves.

The tests that matter here are the ones about *harm*. A woman can lose nothing
by attempting a task: a failure writes no evidence and cannot touch a skill an
employer already verified. She cannot be told she passed when nobody looked.
She cannot mark her own work. And nobody can read what she wrote by changing an
id in a URL.

The model is off for the whole suite (`conftest.disable_llm`), so a submission
to an AI-reviewed task lands in `submitted` and waits — which is the behaviour
under a provider outage, and worth having as the default the tests see.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.constants import (
    EvaluatorKind,
    EvidenceKind,
    Language,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
    Role,
    SkillCategory,
    SkillStatus,
    TaskStatus,
    TaskSubmissionKind,
)
from app.core.security import create_token
from app.models.practice import PracticalTask, TaskAttempt
from app.models.program import Program
from app.models.skill import Skill, SkillEvidence, UserSkill
from app.models.user import User
from app.services import practice, skills

TASKS = "/api/v1/practical-tasks"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


def _headers(user_id: uuid.UUID, *roles: Role) -> dict[str, str]:
    primary = roles[0] if roles else Role.USER
    token = create_token(
        user_id, primary, "access", {"roles": [role.value for role in (roles or (Role.USER,))]}
    )
    return {"Authorization": f"Bearer {token}"}


async def _account(session, user_id: uuid.UUID) -> None:
    session.add(User(id=user_id, phone=_phone(), language=Language.UZ))
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


async def _task(
    session,
    *,
    slug: str | None = None,
    kind: TaskSubmissionKind = TaskSubmissionKind.TEXT,
    published: bool = True,
    skills_practised: list[str] | None = None,
    level: ProficiencyLevel | None = ProficiencyLevel.ELEMENTARY,
    min_chars: int | None = 20,
    fields: list | None = None,
    ai_reviewed: bool = True,
    program: Program | None = None,
) -> PracticalTask:
    task = PracticalTask(
        slug=slug or f"task-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Topshiriq", "ru": "Задание", "en": "Task"},
        summary_i18n={"uz": "Qisqacha", "en": "Summary"},
        instructions_i18n={"uz": "Koʻrsatma", "en": "Instructions"},
        outcome_i18n={"uz": "Natija", "en": "Outcome"},
        criteria=[{"key": "clear", "text_i18n": {"uz": "Aniq", "en": "Clear"}}],
        kind=kind,
        fields=fields or [],
        min_chars=min_chars,
        level=level,
        estimated_minutes=30,
        skills_practised=skills_practised if skills_practised is not None else ["Excel"],
        program_id=program.id if program else None,
        ai_reviewed=ai_reviewed,
        is_published=published,
        published_at=datetime.now(UTC),
    )
    session.add(task)
    await session.flush()
    return task


async def _course(session) -> Program:
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Kurs", "en": "Course"},
        goal_i18n={},
        description_i18n={},
        category=ProgramCategory.FINANCIAL_LITERACY,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        learning_outcomes=[],
        skills_taught=["Excel"],
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(program)
    await session.flush()
    return program


# --- task access ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_published_task_is_readable_by_anyone(client, session):
    await _skill(session, "excel", "Excel")
    task = await _task(session, slug="byudjet")

    response = await client.get(f"{TASKS}/byudjet")

    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == task.slug
    assert body["criteria"][0]["key"] == "clear"
    assert body["skills"][0]["slug"] == "excel"
    # A visitor has no state on it, and is not told she has none of something.
    assert body["status"] is None
    assert body["my_attempts"] == []


@pytest.mark.asyncio
async def test_an_unpublished_task_is_not_readable_and_cannot_be_started(client, session, user_id):
    await _account(session, user_id)
    await _task(session, slug="qoralama", published=False)

    assert (await client.get(f"{TASKS}/qoralama")).status_code == 404
    assert (await client.get(f"{TASKS}/no-such-task")).status_code == 404
    started = await client.post(f"{TASKS}/qoralama/start", headers=_headers(user_id))
    assert started.status_code == 404


@pytest.mark.asyncio
async def test_the_catalogue_filters_by_skill_across_spellings(client, session):
    """`эксель` and `Excel` are one skill, so one task answers both."""
    skill = await _skill(session, "excel", "Excel")
    skill.aliases = [*skill.aliases, "эксель"]
    await _task(session, slug="excel-ish", skills_practised=["Excel"])
    await _task(session, slug="boshqa", skills_practised=["Tikuvchilik"])
    await session.flush()

    latin = await client.get(f"{TASKS}?skill=Excel")
    cyrillic = await client.get(f"{TASKS}?skill=эксель")

    assert [row["slug"] for row in latin.json()] == ["excel-ish"]
    assert [row["slug"] for row in cyrillic.json()] == ["excel-ish"]
    assert (await client.get(f"{TASKS}?skill=nothing-practises-this")).json() == []


@pytest.mark.asyncio
async def test_starting_and_my_tasks_need_an_account(client, session):
    await _task(session, slug="ish")

    assert (await client.post(f"{TASKS}/ish/start")).status_code == 401
    assert (await client.post(f"{TASKS}/ish/submit", json={"text": "x" * 50})).status_code == 401
    assert (await client.get(f"{TASKS}/me")).status_code == 401


# --- submission -------------------------------------------------------------


@pytest.mark.asyncio
async def test_starting_twice_is_the_same_attempt(client, session, user_id):
    await _account(session, user_id)
    await _task(session, slug="ish")

    first = await client.post(f"{TASKS}/ish/start", headers=_headers(user_id))
    second = await client.post(f"{TASKS}/ish/start", headers=_headers(user_id))

    assert first.status_code == 200
    assert second.status_code == 200
    rows = list(
        (await session.execute(select(TaskAttempt).where(TaskAttempt.user_id == user_id))).scalars()
    )
    assert len(rows) == 1
    assert rows[0].status == TaskStatus.STARTED


@pytest.mark.asyncio
async def test_a_submission_persists_and_waits_rather_than_claiming_a_pass(
    client, session, user_id
):
    """With no model reachable the attempt is `submitted`, not `passed`. The
    one thing this system may never do is say she passed when nobody looked."""
    await _account(session, user_id)
    await _task(session, slug="ish", min_chars=20)

    response = await client.post(
        f"{TASKS}/ish/submit",
        json={"text": "Men byudjetimni yozdim." * 3},
        headers=_headers(user_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "submitted"
    [attempt] = body["my_attempts"]
    assert attempt["status"] == "submitted"
    assert attempt["evaluation"] is None
    assert attempt["submitted_at"] is not None
    # And it survives a reload — it is a row, not a screen state.
    again = await client.get(f"{TASKS}/ish", headers=_headers(user_id))
    assert again.json()["my_attempts"][0]["status"] == "submitted"


@pytest.mark.asyncio
async def test_a_submission_that_does_not_answer_the_brief_is_refused(client, session, user_id):
    await _account(session, user_id)
    await _task(session, slug="matn", min_chars=200)
    await _task(session, slug="havola", kind=TaskSubmissionKind.LINK)

    short = await client.post(
        f"{TASKS}/matn/submit", json={"text": "qisqa"}, headers=_headers(user_id)
    )
    empty = await client.post(f"{TASKS}/matn/submit", json={}, headers=_headers(user_id))
    bad = await client.post(
        f"{TASKS}/havola/submit", json={"link": "javascript:alert(1)"}, headers=_headers(user_id)
    )
    not_a_url = await client.post(
        f"{TASKS}/havola/submit", json={"link": "just some words"}, headers=_headers(user_id)
    )

    assert short.status_code == 422
    assert short.json()["detail"]["reason"] == "too_short"
    assert empty.status_code == 422
    assert bad.status_code == 422
    assert bad.json()["detail"]["reason"] == "bad_link"
    assert not_a_url.status_code == 422
    # Nothing was filed.
    assert (
        await session.scalar(select(TaskAttempt.id).where(TaskAttempt.user_id == user_id))
    ) is None


@pytest.mark.asyncio
async def test_a_fields_task_requires_every_field(client, session, user_id):
    await _account(session, user_id)
    await _task(
        session,
        slug="uch-savol",
        kind=TaskSubmissionKind.FIELDS,
        fields=[
            {"key": "a", "label_i18n": {"uz": "A"}, "min_chars": 10},
            {"key": "b", "label_i18n": {"uz": "B"}, "min_chars": 10},
        ],
    )

    partial = await client.post(
        f"{TASKS}/uch-savol/submit",
        json={"fields": {"a": "javob bering"}},
        headers=_headers(user_id),
    )
    full = await client.post(
        f"{TASKS}/uch-savol/submit",
        json={"fields": {"a": "birinchi javob", "b": "ikkinchi javob"}},
        headers=_headers(user_id),
    )

    assert partial.status_code == 422
    assert partial.json()["detail"]["field"] == "b"
    assert full.status_code == 200
    assert full.json()["my_attempts"][0]["submission"]["fields"]["b"] == "ikkinchi javob"


@pytest.mark.asyncio
async def test_one_womans_submission_is_never_visible_to_another(client, session, user_id):
    """There is no endpoint that takes a user id, so there is no id to change."""
    await _account(session, user_id)
    stranger = uuid.uuid4()
    await _account(session, stranger)
    task = await _task(session, slug="ish")
    session.add(
        TaskAttempt(
            task_id=task.id,
            user_id=stranger,
            attempt_no=1,
            status=TaskStatus.SUBMITTED,
            submission={"text": "her private answer"},
            submitted_at=datetime.now(UTC),
        )
    )
    await session.flush()

    detail = (await client.get(f"{TASKS}/ish", headers=_headers(user_id))).json()
    mine = (await client.get(f"{TASKS}/me", headers=_headers(user_id))).json()

    assert detail["my_attempts"] == []
    assert detail["status"] is None
    assert mine == []


@pytest.mark.asyncio
async def test_an_ordinary_user_cannot_read_the_review_queue(client, session, user_id):
    await _account(session, user_id)
    stranger = uuid.uuid4()
    await _account(session, stranger)
    task = await _task(session, slug="ish")
    session.add(
        TaskAttempt(
            task_id=task.id,
            user_id=stranger,
            attempt_no=1,
            status=TaskStatus.SUBMITTED,
            submission={"text": "private"},
            submitted_at=datetime.now(UTC),
        )
    )
    await session.flush()

    assert (await client.get(f"{TASKS}/review", headers=_headers(user_id))).status_code == 403
    assert (await client.get(f"{TASKS}/review")).status_code == 401


# --- evaluation -------------------------------------------------------------


async def _submitted(session, client, user_id, **kwargs) -> tuple[PracticalTask, TaskAttempt]:
    task = await _task(session, **kwargs)
    await client.post(
        f"{TASKS}/{task.slug}/submit",
        json={"text": "Men topshiriqni bajardim va hisobotni yozdim."},
        headers=_headers(user_id),
    )
    attempt = await session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.id, TaskAttempt.user_id == user_id)
    )
    return task, attempt


@pytest.mark.asyncio
async def test_only_an_authorized_role_may_evaluate(client, session, user_id):
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    _, attempt = await _submitted(session, client, user_id, slug="ish")

    outsider = uuid.uuid4()
    await _account(session, outsider)
    verdict = {"passed": True, "score": 100, "feedback": "Yaxshi"}

    refused = await client.post(
        f"{TASKS}/attempts/{attempt.id}/evaluate", json=verdict, headers=_headers(outsider)
    )
    unauthenticated = await client.post(f"{TASKS}/attempts/{attempt.id}/evaluate", json=verdict)

    assert refused.status_code == 403
    assert unauthenticated.status_code == 401
    await session.refresh(attempt)
    assert attempt.passed is None
    assert attempt.status == TaskStatus.SUBMITTED


@pytest.mark.asyncio
async def test_a_woman_cannot_mark_her_own_work_however_senior_she_is(client, session, user_id):
    """A trainer taking a course is a learner on that course."""
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    _, attempt = await _submitted(session, client, user_id, slug="ish")

    response = await client.post(
        f"{TASKS}/attempts/{attempt.id}/evaluate",
        json={"passed": True, "score": 100, "feedback": "Men oʻzim baholadim"},
        headers=_headers(user_id, Role.ADMIN, Role.TRAINER, Role.MENTOR),
    )

    assert response.status_code == 403
    await session.refresh(attempt)
    assert attempt.passed is None


@pytest.mark.asyncio
async def test_nothing_may_be_evaluated_before_it_is_submitted(client, session, user_id):
    await _account(session, user_id)
    task = await _task(session, slug="ish")
    await client.post(f"{TASKS}/ish/start", headers=_headers(user_id))
    attempt = await session.scalar(
        select(TaskAttempt).where(TaskAttempt.task_id == task.id, TaskAttempt.user_id == user_id)
    )

    trainer = uuid.uuid4()
    await _account(session, trainer)
    response = await client.post(
        f"{TASKS}/attempts/{attempt.id}/evaluate",
        json={"passed": True, "feedback": "x"},
        headers=_headers(trainer, Role.TRAINER),
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_a_pass_and_a_needs_improvement_both_persist(client, session, user_id):
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    trainer = uuid.uuid4()
    await _account(session, trainer)

    _, failed = await _submitted(session, client, user_id, slug="birinchi")
    response = await client.post(
        f"{TASKS}/attempts/{failed.id}/evaluate",
        json={
            "passed": False,
            "score": 50,
            "feedback": "Jamgʻarma summasi yoʻq.",
            "criteria_met": [{"key": "clear", "met": False, "note": "Aniq emas"}],
        },
        headers=_headers(trainer, Role.TRAINER),
    )

    assert response.status_code == 200
    # The evaluator gets back the one attempt they signed, not her history.
    body = response.json()
    assert body["attempt"]["id"] == str(failed.id)
    assert "my_attempts" not in body["attempt"]
    assert body["task"]["my_attempts"] == []

    await session.refresh(failed)
    assert failed.status == TaskStatus.NEEDS_IMPROVEMENT
    assert failed.passed is False
    assert failed.feedback == "Jamgʻarma summasi yoʻq."
    assert failed.evaluator_id == trainer

    body = (await client.get(f"{TASKS}/birinchi", headers=_headers(user_id))).json()
    assert body["status"] == "needs_improvement"
    assert body["my_attempts"][0]["evaluation"]["feedback"] == "Jamgʻarma summasi yoʻq."
    assert body["my_attempts"][0]["evaluation"]["criteria_met"][0]["met"] is False
    # Nothing was proven, so nothing is named as proven.
    assert body["my_attempts"][0]["evaluation"]["skills_evidenced"] == []
    # She may go again.
    assert body["can_start"] is True


# --- skills -----------------------------------------------------------------


async def _evidence(session, user_id: uuid.UUID) -> list[SkillEvidence]:
    rows = await session.execute(
        select(SkillEvidence)
        .join(UserSkill, UserSkill.id == SkillEvidence.user_skill_id)
        .where(UserSkill.user_id == user_id)
    )
    return list(rows.scalars())


@pytest.mark.asyncio
async def test_a_passed_task_makes_a_skill_assessed_and_never_verified(client, session, user_id):
    """A platform assessment says she applied it. Only a person verifies."""
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    trainer = uuid.uuid4()
    await _account(session, trainer)
    _, attempt = await _submitted(session, client, user_id, slug="ish")

    await client.post(
        f"{TASKS}/attempts/{attempt.id}/evaluate",
        json={"passed": True, "score": 100, "feedback": "Barcha mezonlar bajarilgan."},
        headers=_headers(trainer, Role.TRAINER),
    )

    [evidence] = await _evidence(session, user_id)
    assert evidence.kind == EvidenceKind.FORMAL_ASSESSMENT
    assert evidence.source_type == "task_attempt"
    assert evidence.source_id == str(attempt.id)
    assert evidence.level == ProficiencyLevel.ELEMENTARY

    record = await session.scalar(select(UserSkill).where(UserSkill.user_id == user_id))
    assert record.status == SkillStatus.ASSESSED
    assert record.status != SkillStatus.VERIFIED


@pytest.mark.asyncio
async def test_a_mentor_signing_the_same_work_verifies_it(client, session, user_id):
    """The evidence follows who assessed it — that is the Step 3 rule, not a
    new one. A mentor putting their name to the work is a stronger claim."""
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    mentor = uuid.uuid4()
    await _account(session, mentor)
    _, attempt = await _submitted(session, client, user_id, slug="ish")

    await client.post(
        f"{TASKS}/attempts/{attempt.id}/evaluate",
        json={"passed": True, "score": 100, "feedback": "Men koʻrdim, ishlaydi."},
        headers=_headers(mentor, Role.MENTOR),
    )

    [evidence] = await _evidence(session, user_id)
    assert evidence.kind == EvidenceKind.MENTOR_ASSESSMENT
    assert evidence.verified_by_id == mentor
    record = await session.scalar(select(UserSkill).where(UserSkill.user_id == user_id))
    assert record.status == SkillStatus.VERIFIED


@pytest.mark.asyncio
async def test_a_failed_task_never_costs_her_a_verified_skill(client, session, user_id):
    """The rule this whole module turns on. Failing a test you volunteered for
    must not take away something you had already proven."""
    await _account(session, user_id)
    skill = await _skill(session, "excel", "Excel")
    await skills.record_evidence(
        session,
        user_id=user_id,
        skill=skill,
        kind=EvidenceKind.EMPLOYER_ASSESSMENT,
        source_type="employer",
        source_id=uuid.uuid4().hex,
        level=ProficiencyLevel.INTERMEDIATE,
    )
    await session.flush()

    trainer = uuid.uuid4()
    await _account(session, trainer)
    _, attempt = await _submitted(session, client, user_id, slug="ish")
    await client.post(
        f"{TASKS}/attempts/{attempt.id}/evaluate",
        json={"passed": False, "score": 0, "feedback": "Qayta urinib koʻring."},
        headers=_headers(trainer, Role.TRAINER),
    )

    record = await session.scalar(select(UserSkill).where(UserSkill.user_id == user_id))
    assert record.status == SkillStatus.VERIFIED
    assert record.level == ProficiencyLevel.INTERMEDIATE
    # And the failure wrote nothing at all — not a weaker row, nothing.
    evidence = await _evidence(session, user_id)
    assert [item.kind for item in evidence] == [EvidenceKind.EMPLOYER_ASSESSMENT]
    # The attempt is still on the record, as work she did.
    await session.refresh(attempt)
    assert attempt.status == TaskStatus.NEEDS_IMPROVEMENT


@pytest.mark.asyncio
async def test_re_evaluating_one_attempt_does_not_stack_evidence(client, session, user_id):
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    trainer = uuid.uuid4()
    await _account(session, trainer)
    _, attempt = await _submitted(session, client, user_id, slug="ish")

    for _ in range(3):
        await client.post(
            f"{TASKS}/attempts/{attempt.id}/evaluate",
            json={"passed": True, "score": 100, "feedback": "Yaxshi"},
            headers=_headers(trainer, Role.TRAINER),
        )

    evidence = await _evidence(session, user_id)
    assert len(evidence) == 1


@pytest.mark.asyncio
async def test_a_task_that_practises_nothing_writes_no_evidence(client, session, user_id):
    await _account(session, user_id)
    trainer = uuid.uuid4()
    await _account(session, trainer)
    _, attempt = await _submitted(session, client, user_id, slug="ish", skills_practised=[])

    await client.post(
        f"{TASKS}/attempts/{attempt.id}/evaluate",
        json={"passed": True, "score": 100, "feedback": "Yaxshi"},
        headers=_headers(trainer, Role.TRAINER),
    )

    assert await _evidence(session, user_id) == []


# --- retry ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retry_is_a_new_attempt_and_keeps_the_first(client, session, user_id):
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    trainer = uuid.uuid4()
    await _account(session, trainer)
    _, first = await _submitted(session, client, user_id, slug="ish")
    await client.post(
        f"{TASKS}/attempts/{first.id}/evaluate",
        json={"passed": False, "score": 0, "feedback": "Jamgʻarma yoʻq."},
        headers=_headers(trainer, Role.TRAINER),
    )

    retried = await client.post(
        f"{TASKS}/ish/submit",
        json={"text": "Bu safar jamgʻarma summasini ham yozdim."},
        headers=_headers(user_id),
    )

    assert retried.status_code == 200
    body = retried.json()
    assert [a["attempt_no"] for a in body["my_attempts"]] == [2, 1]
    # The first attempt and its feedback are still there — that is the story.
    assert body["my_attempts"][1]["evaluation"]["feedback"] == "Jamgʻarma yoʻq."
    assert body["my_attempts"][0]["status"] == "submitted"


@pytest.mark.asyncio
async def test_a_passed_task_cannot_be_attempted_again(client, session, user_id):
    """Nothing left to prove, and a second verdict could only take away."""
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    trainer = uuid.uuid4()
    await _account(session, trainer)
    _, attempt = await _submitted(session, client, user_id, slug="ish")
    await client.post(
        f"{TASKS}/attempts/{attempt.id}/evaluate",
        json={"passed": True, "score": 100, "feedback": "Yaxshi"},
        headers=_headers(trainer, Role.TRAINER),
    )

    again = await client.post(f"{TASKS}/ish/start", headers=_headers(user_id))

    assert again.status_code == 409
    assert again.json()["detail"] == "passed"
    body = (await client.get(f"{TASKS}/ish", headers=_headers(user_id))).json()
    assert body["status"] == "passed"
    assert body["can_start"] is False


@pytest.mark.asyncio
async def test_a_submission_awaiting_review_cannot_be_replaced(client, session, user_id):
    await _account(session, user_id)
    await _task(session, slug="ish")
    await client.post(
        f"{TASKS}/ish/submit", json={"text": "birinchi javobim bu yerda"}, headers=_headers(user_id)
    )

    again = await client.post(f"{TASKS}/ish/start", headers=_headers(user_id))

    assert again.status_code == 409
    assert again.json()["detail"] == "awaiting_review"


# --- the AI reviewer --------------------------------------------------------


class _Gateway:
    """A reviewer that says whatever the test wants it to say."""

    enabled = True

    def __init__(self, payload: dict | None):
        self.payload = payload
        self.prompts: list[str] = []

    @staticmethod
    def sanitise(text: str) -> str:
        return text

    async def complete(self, *, system, messages, **kwargs):
        from app.services.llm_gateway import LlmResponse

        self.prompts.append(messages[0]["content"])
        return LlmResponse(
            text="",
            parsed=self.payload,
            model="test-model",
            prompt_version="test",
            trace_id=uuid.uuid4().hex,
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
            refused=False,
        )


@pytest.mark.asyncio
async def test_the_ai_reviewer_sees_the_brief_and_the_work_and_nothing_about_her(session, user_id):
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    task = await _task(session, slug="ish")
    gateway = _Gateway(
        {
            "passed": True,
            "score": 100,
            "feedback": "Barcha mezonlar bajarilgan.",
            "criteria_met": [{"key": "clear", "met": True, "note": "Aniq yozilgan"}],
        }
    )

    from app.schemas.practice import TaskSubmitIn

    await practice.submit(
        session,
        user_id=user_id,
        task=task,
        payload=TaskSubmitIn(text="Men byudjetimni toʻliq yozdim."),
        gateway=gateway,
    )

    [prompt] = gateway.prompts
    assert "CRITERIA" in prompt
    assert "Men byudjetimni toʻliq yozdim." in prompt
    # It knows the work and nothing about the woman who wrote it.
    assert str(user_id) not in prompt

    record = await session.scalar(select(UserSkill).where(UserSkill.user_id == user_id))
    assert record.status == SkillStatus.ASSESSED
    [evidence] = await _evidence(session, user_id)
    assert evidence.kind == EvidenceKind.AI_ASSESSMENT


@pytest.mark.asyncio
async def test_a_criterion_the_reviewer_invented_is_dropped_and_a_skipped_one_is_unmet(
    session, user_id
):
    """The verdict is recomputed from the task's own criteria, so a pass can
    never rest on a criterion that does not exist."""
    await _account(session, user_id)
    task = await _task(session, slug="ish")
    task.criteria = [
        {"key": "clear", "text_i18n": {"uz": "Aniq"}},
        {"key": "numbers", "text_i18n": {"uz": "Raqamlar"}},
    ]
    await session.flush()

    gateway = _Gateway(
        {
            "passed": True,  # the model's own claim, which is not trusted
            "score": 100,
            "feedback": "Ajoyib",
            "criteria_met": [
                {"key": "clear", "met": True, "note": ""},
                {"key": "invented", "met": True, "note": "not a real criterion"},
            ],
        }
    )
    verdict = await practice.ai_review(task, TaskAttempt(submission={"text": "x"}), gateway=gateway)

    assert [row["key"] for row in verdict["criteria_met"]] == ["clear", "numbers"]
    assert verdict["criteria_met"][1]["met"] is False
    # One of two met, so it did not pass whatever the model asserted.
    assert verdict["passed"] is False
    assert verdict["score"] == 50.0


@pytest.mark.asyncio
async def test_when_the_reviewer_is_unreachable_the_work_waits_for_a_person(session, user_id):
    await _account(session, user_id)
    task = await _task(session, slug="ish")

    from app.schemas.practice import TaskSubmitIn

    attempt = await practice.submit(
        session,
        user_id=user_id,
        task=task,
        payload=TaskSubmitIn(text="Men topshiriqni bajardim."),
        gateway=_Gateway(None),
    )

    assert attempt.status == TaskStatus.SUBMITTED
    assert attempt.passed is None
    assert await _evidence(session, user_id) == []


@pytest.mark.asyncio
async def test_feedback_is_written_in_the_language_she_reads(session, user_id):
    """A correction is only useful if she can read it. The task's authoring
    language is not necessarily hers."""
    await _account(session, user_id)
    task = await _task(session, slug="ish")
    gateway = _Gateway({"passed": True, "score": 100, "feedback": "ok", "criteria_met": []})

    from app.schemas.practice import TaskSubmitIn

    await practice.submit(
        session,
        user_id=user_id,
        task=task,
        payload=TaskSubmitIn(text="Menign javobim toʻliq yozilgan."),
        language="ru",
        gateway=gateway,
    )

    [prompt] = gateway.prompts
    assert "ANSWER LANGUAGE: ru" in prompt


@pytest.mark.asyncio
async def test_a_task_with_no_criteria_is_never_auto_assessed(session, user_id):
    """Marking work against a standard nobody wrote down is not assessment."""
    await _account(session, user_id)
    task = await _task(session, slug="ish")
    task.criteria = []
    await session.flush()

    verdict = await practice.ai_review(
        task, TaskAttempt(submission={"text": "x"}), gateway=_Gateway({"passed": True})
    )

    assert verdict is None


# --- the review queue -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_evaluator_sees_the_work_and_the_brief_but_not_her_name(client, session, user_id):
    await _account(session, user_id)
    await _task(session, slug="ish", ai_reviewed=False)
    await client.post(
        f"{TASKS}/ish/submit",
        json={"text": "Mening javobim shu yerda, toʻliq."},
        headers=_headers(user_id),
    )

    trainer = uuid.uuid4()
    await _account(session, trainer)
    response = await client.get(f"{TASKS}/review", headers=_headers(trainer, Role.TRAINER))

    assert response.status_code == 200
    [item] = response.json()
    assert item["attempt"]["submission"]["text"].startswith("Mening javobim")
    assert item["task"]["slug"] == "ish"
    # Nothing identifies who wrote it.
    assert "user_id" not in item["attempt"]
    assert str(user_id) not in str(item)


@pytest.mark.asyncio
async def test_my_tasks_puts_what_needs_her_first(client, session, user_id):
    await _account(session, user_id)
    await _skill(session, "excel", "Excel")
    trainer = uuid.uuid4()
    await _account(session, trainer)

    _, passed = await _submitted(session, client, user_id, slug="tugagan")
    await client.post(
        f"{TASKS}/attempts/{passed.id}/evaluate",
        json={"passed": True, "score": 100, "feedback": "Yaxshi"},
        headers=_headers(trainer, Role.TRAINER),
    )
    _, failed = await _submitted(session, client, user_id, slug="qayta")
    await client.post(
        f"{TASKS}/attempts/{failed.id}/evaluate",
        json={"passed": False, "score": 0, "feedback": "Qayta koʻring"},
        headers=_headers(trainer, Role.TRAINER),
    )
    await client.post(f"{TASKS}/kutmoqda/start", headers=_headers(user_id))  # 404, ignored
    await _task(session, slug="ochiq")
    await client.post(f"{TASKS}/ochiq/start", headers=_headers(user_id))

    body = (await client.get(f"{TASKS}/me", headers=_headers(user_id))).json()

    assert [row["status"] for row in body] == ["needs_improvement", "started", "passed"]


# --- authoring --------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_content_roles_may_author_a_task(client, session, user_id):
    await _account(session, user_id)
    payload = {
        "slug": "yangi-topshiriq",
        "title_i18n": {"uz": "Yangi"},
        "criteria": [{"key": "a", "text_i18n": {"uz": "A"}}],
        "skills_practised": ["Excel"],
        "is_published": True,
    }

    refused = await client.post(TASKS, json=payload, headers=_headers(user_id))
    trainer = uuid.uuid4()
    await _account(session, trainer)
    allowed = await client.post(TASKS, json=payload, headers=_headers(trainer, Role.TRAINER))

    assert refused.status_code == 403
    assert allowed.status_code == 201
    assert allowed.json()["slug"] == "yangi-topshiriq"
    duplicate = await client.post(TASKS, json=payload, headers=_headers(trainer, Role.TRAINER))
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_the_evaluator_kind_map_never_lets_the_platform_verify():
    """Read as a rule rather than by walking the code: only a person outside
    the platform's own staff produces verified evidence."""
    for kind, evidence in practice.EVALUATOR_EVIDENCE.items():
        supports = skills.EVIDENCE_STATUS[evidence]
        if kind in (EvaluatorKind.AI, EvaluatorKind.TRAINER):
            assert supports == SkillStatus.ASSESSED
        else:
            assert supports == SkillStatus.VERIFIED
