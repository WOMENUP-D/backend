"""Practical tasks: doing the work, being assessed on it, and what that proves.

The learning section could show that a woman was *taught* something. This is
the layer that can show she can *do* it — and the distance between those two
claims is the whole point of the module.

**The evidence a pass writes is decided by who assessed it, never by the task.**
A platform assessment — this module's own AI reviewer, or a trainer signing it
off — says she applied what she learned: that is `assessed`. A mentor or a
partner organisation putting their name to the same piece of work is a stronger
claim about the world, and that is `verified`. Both go through
`services.skills`, which has always been the only thing that decides what a
piece of evidence is worth. This module chooses the *kind*; it never chooses
the status.

**A failed attempt writes nothing.** Not a downgrade, not a weaker row —
nothing. `derive_state` takes the strongest claim her surviving evidence
supports, so a woman whose Excel was verified by an employer and who then
fumbles a practice task still has verified Excel, plus an honest record that
one attempt needs improvement. Failing a test you volunteered for must never
cost you something you had already proven.

**Submitted is not passed.** An attempt sits in `submitted` until an evaluation
is actually recorded. When the model is unreachable it stays there and says so,
because the one thing a skills system may not do is tell a woman she passed
when nobody looked.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    EvaluatorKind,
    EvidenceKind,
    ProficiencyLevel,
    Role,
    TaskStatus,
    TaskSubmissionKind,
)
from app.models.practice import PracticalTask, TaskAttempt
from app.models.program import Program
from app.schemas.practice import (
    CriterionVerdict,
    PracticalTaskDetail,
    PracticalTaskRead,
    TaskAttemptRead,
    TaskCriterion,
    TaskEvaluationRead,
    TaskField,
    TaskSubmitIn,
)
from app.schemas.skill import SkillRef
from app.services import skills as skill_service
from app.services.llm_gateway import LlmUnavailableError, llm_gateway
from app.services.prompts import PRACTICE_REVIEW_SCHEMA, PRACTICE_REVIEW_SYSTEM

logger = logging.getLogger(__name__)

CATALOGUE_LIMIT = 100
REVIEW_LIMIT = 50
#: Long enough that an empty box cannot be filed as work, short enough that it
#: never becomes the real test. It is not an assessment and is never reported
#: as one.
DEFAULT_MIN_CHARS = 120

#: What each kind of evaluator's verdict is worth, in the vocabulary
#: `services.skills` already speaks.
#:
#: The split is the Step 3 rule restated, not a new one: the platform assessing
#: her own work is `assessed`; somebody outside it putting their name to the
#: same work is `verified`. A trainer is the person who wrote the course, which
#: makes their sign-off a formal assessment rather than an outside opinion.
EVALUATOR_EVIDENCE: dict[EvaluatorKind, EvidenceKind] = {
    EvaluatorKind.AI: EvidenceKind.AI_ASSESSMENT,  # -> assessed
    EvaluatorKind.TRAINER: EvidenceKind.FORMAL_ASSESSMENT,  # -> assessed
    EvaluatorKind.MENTOR: EvidenceKind.MENTOR_ASSESSMENT,  # -> verified
    EvaluatorKind.PARTNER: EvidenceKind.EMPLOYER_ASSESSMENT,  # -> verified
}

#: Which role signs as what. Read in order: the strongest claim a woman's
#: roles entitle her to make about somebody else's work.
ROLE_EVALUATOR: list[tuple[Role, EvaluatorKind]] = [
    (Role.MENTOR, EvaluatorKind.MENTOR),
    (Role.PARTNER, EvaluatorKind.PARTNER),
    (Role.TRAINER, EvaluatorKind.TRAINER),
    (Role.MODERATOR, EvaluatorKind.TRAINER),
    (Role.ADMIN, EvaluatorKind.TRAINER),
]

_OPEN = (TaskStatus.STARTED, TaskStatus.SUBMITTED)


class SubmissionError(ValueError):
    """The submission does not answer the brief. Carries a machine-readable
    reason so the browser can say which field, in her language."""

    def __init__(self, reason: str, field: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field


def _text(values: dict, language: str) -> str:
    return values.get(language) or values.get("uz") or next(iter(values.values()), "")


# ---------------------------------------------------------------------------
# Reading tasks
# ---------------------------------------------------------------------------


async def catalogue(
    session: AsyncSession,
    *,
    level: ProficiencyLevel | None = None,
    program_id: uuid.UUID | None = None,
    search: str | None = None,
    published_only: bool = True,
    limit: int = CATALOGUE_LIMIT,
) -> list[PracticalTask]:
    """Published tasks, in curation order."""
    stmt = select(PracticalTask)
    if published_only:
        stmt = stmt.where(PracticalTask.is_published.is_(True))
    if level is not None:
        stmt = stmt.where(PracticalTask.level == level)
    if program_id is not None:
        stmt = stmt.where(PracticalTask.program_id == program_id)
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                *(
                    func.lower(column.op("->>")(language)).like(pattern)
                    for column in (PracticalTask.title_i18n, PracticalTask.summary_i18n)
                    for language in ("uz", "ru", "en")
                ),
                func.lower(PracticalTask.slug).like(pattern),
                func.lower(func.array_to_string(PracticalTask.skills_practised, " ")).like(pattern),
            )
        )
    rows = await session.execute(
        stmt.order_by(PracticalTask.order_index, PracticalTask.slug).limit(limit)
    )
    return list(rows.scalars())


async def by_slug(
    session: AsyncSession, slug: str, *, published_only: bool = True
) -> PracticalTask | None:
    stmt = select(PracticalTask).where(PracticalTask.slug == slug)
    if published_only:
        stmt = stmt.where(PracticalTask.is_published.is_(True))
    return await session.scalar(stmt)


async def attempts_for(
    session: AsyncSession, user_id: uuid.UUID, task_ids: Iterable[uuid.UUID] | None = None
) -> dict[uuid.UUID, list[TaskAttempt]]:
    """Her attempts, newest first, by task. Only ever her own."""
    stmt = select(TaskAttempt).where(TaskAttempt.user_id == user_id)
    ids = list(task_ids) if task_ids is not None else None
    if ids is not None:
        if not ids:
            return {}
        stmt = stmt.where(TaskAttempt.task_id.in_(ids))

    out: dict[uuid.UUID, list[TaskAttempt]] = {}
    rows = await session.execute(stmt.order_by(TaskAttempt.attempt_no.desc()))
    for attempt in rows.scalars():
        out.setdefault(attempt.task_id, []).append(attempt)
    return out


async def index_for(
    session: AsyncSession, tasks: Iterable[PracticalTask]
) -> skill_service.SkillIndex:
    """One taxonomy lookup covering every skill a set of tasks practises."""
    labels = {label for task in tasks for label in (task.skills_practised or [])}
    return await skill_service.SkillIndex.load(session, labels)


def current_status(attempts: Sequence[TaskAttempt]) -> TaskStatus | None:
    """Where she stands on a task, from her attempts.

    A pass, once earned, is where she stands — a later attempt cannot take it
    away. Otherwise the newest attempt speaks.
    """
    if not attempts:
        return None
    if any(attempt.status == TaskStatus.PASSED for attempt in attempts):
        return TaskStatus.PASSED
    return attempts[0].status


def can_start(attempts: Sequence[TaskAttempt]) -> bool:
    """Whether a fresh attempt is hers to make.

    Never while one is open — that would be two answers to one brief — and
    never once she has passed, because there is nothing left to prove and a
    second verdict could only take something away.
    """
    if not attempts:
        return True
    if any(attempt.status == TaskStatus.PASSED for attempt in attempts):
        return False
    return not any(attempt.status in _OPEN for attempt in attempts)


def open_attempt(attempts: Sequence[TaskAttempt]) -> TaskAttempt | None:
    """The attempt she is working on, if any. `submitted` still counts as open
    until it is assessed, but it may no longer be edited."""
    return next((a for a in attempts if a.status == TaskStatus.STARTED), None)


# ---------------------------------------------------------------------------
# Composing what the API returns
# ---------------------------------------------------------------------------


def _evaluation(
    attempt: TaskAttempt, task: PracticalTask, index: skill_service.SkillIndex
) -> TaskEvaluationRead | None:
    """The verdict, or nothing. Absence is how "not assessed yet" is said."""
    if attempt.passed is None or attempt.evaluated_at is None:
        return None
    return TaskEvaluationRead(
        passed=attempt.passed,
        score=attempt.score,
        feedback=attempt.feedback or "",
        criteria_met=[CriterionVerdict(**row) for row in (attempt.criteria_met or [])],
        evaluator_kind=attempt.evaluator_kind,
        evaluated_at=attempt.evaluated_at,
        # Only a pass writes evidence, so only a pass names any.
        skills_evidenced=(index.refs(task.skills_practised or []) if attempt.passed else []),
    )


def attempt_read(
    attempt: TaskAttempt, task: PracticalTask, index: skill_service.SkillIndex
) -> TaskAttemptRead:
    return TaskAttemptRead(
        id=attempt.id,
        attempt_no=attempt.attempt_no,
        status=attempt.status,
        submission=attempt.submission or {},
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        evaluation=_evaluation(attempt, task, index),
    )


def read(
    task: PracticalTask,
    *,
    index: skill_service.SkillIndex,
    attempts: Sequence[TaskAttempt] = (),
    held: set[str] | None = None,
    program: Program | None = None,
) -> PracticalTaskRead:
    labels = task.skills_practised or []
    skills = index.refs(labels)
    return PracticalTaskRead(
        id=task.id,
        slug=task.slug,
        title_i18n=task.title_i18n,
        summary_i18n=task.summary_i18n,
        kind=task.kind,
        level=task.level,
        estimated_minutes=task.estimated_minutes,
        skills=skills,
        new_skills=([ref for ref in skills if index.key(ref.label) not in held] if held else []),
        program_id=task.program_id,
        program_slug=program.slug if program else None,
        program_title_i18n=program.title_i18n if program else {},
        ai_reviewed=task.ai_reviewed,
        status=current_status(attempts),
        attempts=len(attempts),
    )


def detail(
    task: PracticalTask,
    *,
    index: skill_service.SkillIndex,
    attempts: Sequence[TaskAttempt] = (),
    held: set[str] | None = None,
    program: Program | None = None,
) -> PracticalTaskDetail:
    summary = read(task, index=index, attempts=attempts, held=held, program=program)
    return PracticalTaskDetail(
        **summary.model_dump(),
        instructions_i18n=task.instructions_i18n,
        outcome_i18n=task.outcome_i18n,
        criteria=[TaskCriterion(**row) for row in (task.criteria or [])],
        fields=[TaskField(**row) for row in (task.fields or [])],
        min_chars=task.min_chars,
        my_attempts=[attempt_read(a, task, index) for a in attempts],
        can_start=can_start(attempts),
        can_submit=open_attempt(attempts) is not None,
    )


# ---------------------------------------------------------------------------
# Doing the work
# ---------------------------------------------------------------------------


async def start(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    task: PracticalTask,
    now: datetime | None = None,
) -> TaskAttempt:
    """Open an attempt. Idempotent: an attempt already in progress is returned.

    Raises `SubmissionError` when a fresh attempt is not hers to make — a pass
    is already recorded, or one is awaiting assessment. The server refuses
    whatever the browser believed.
    """
    now = now or datetime.now(UTC)
    mine = (await attempts_for(session, user_id, [task.id])).get(task.id, [])

    existing = open_attempt(mine)
    if existing is not None:
        return existing
    if not can_start(mine):
        raise SubmissionError(
            "passed" if any(a.status == TaskStatus.PASSED for a in mine) else "awaiting_review"
        )

    attempt = TaskAttempt(
        task_id=task.id,
        user_id=user_id,
        attempt_no=(mine[0].attempt_no + 1 if mine else 1),
        status=TaskStatus.STARTED,
        started_at=now,
    )
    session.add(attempt)
    await session.flush()
    return attempt


def validate(task: PracticalTask, payload: TaskSubmitIn) -> dict:
    """Check the work answers the brief, and shape it for storage.

    Server-side and unconditional. What the browser thought was valid is not
    consulted: a submission arriving straight at the API is held to exactly the
    same brief as one typed into the form.
    """
    if task.kind == TaskSubmissionKind.TEXT:
        text = (payload.text or "").strip()
        minimum = task.min_chars if task.min_chars is not None else DEFAULT_MIN_CHARS
        if len(text) < minimum:
            raise SubmissionError("too_short", "text")
        return {"text": text}

    if task.kind == TaskSubmissionKind.LINK:
        link = (payload.link or "").strip()
        parsed = urlparse(link)
        # A scheme and a host, and only the two schemes a browser will open.
        # Anything else is a way of getting a `javascript:` or a `file:` URL in
        # front of whoever reviews it.
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise SubmissionError("bad_link", "link")
        return {"link": link}

    answers: dict[str, str] = {}
    for spec in task.fields or []:
        key = str(spec.get("key") or "")
        value = (payload.fields.get(key) or "").strip()
        if len(value) < int(spec.get("min_chars") or 0) or not value:
            raise SubmissionError("field_too_short", key)
        answers[key] = value
    if not answers:
        raise SubmissionError("empty")
    return {"fields": answers}


async def submit(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    task: PracticalTask,
    payload: TaskSubmitIn,
    language: str | None = None,
    now: datetime | None = None,
    gateway=None,
) -> TaskAttempt:
    """Hand the work in, and assess it when the platform is able to.

    The attempt becomes `submitted` first and stays that way unless an
    evaluation is actually recorded. A task nobody can assess right now is
    waiting for a person, and says so.
    """
    now = now or datetime.now(UTC)
    # Validated before anything is written. A submission that does not answer
    # the brief leaves no trace at all — she has not started the task by
    # failing to submit to it, and a row saying otherwise is a row that has to
    # be explained on her own screen.
    submission = validate(task, payload)

    mine = (await attempts_for(session, user_id, [task.id])).get(task.id, [])
    attempt = open_attempt(mine)
    if attempt is None:
        # Submitting without starting is the ordinary path for a woman who
        # opened the form and wrote straight into it.
        attempt = await start(session, user_id=user_id, task=task, now=now)

    attempt.submission = submission
    attempt.status = TaskStatus.SUBMITTED
    attempt.submitted_at = now
    await session.flush()

    if task.ai_reviewed:
        verdict = await ai_review(task, attempt, language=language, gateway=gateway)
        if verdict is not None:
            await record_evaluation(
                session,
                attempt=attempt,
                task=task,
                passed=verdict["passed"],
                score=verdict["score"],
                feedback=verdict["feedback"],
                criteria_met=verdict["criteria_met"],
                evaluator_kind=EvaluatorKind.AI,
                now=now,
            )
    return attempt


# ---------------------------------------------------------------------------
# Assessing it
# ---------------------------------------------------------------------------


async def ai_review(
    task: PracticalTask,
    attempt: TaskAttempt,
    *,
    language: str | None = None,
    gateway=None,
) -> dict | None:
    """Assess one submission against the task's own criteria.

    The model is shown the brief, the criteria and exactly what she wrote — and
    nothing about her. It cannot flatter a woman it knows nothing about, and it
    cannot hold her history against her.

    `language` is the one she reads in. Feedback is the most useful sentence
    the platform produces, and useful means legible: writing it in whichever
    language the task happened to be authored in would hand a Russian reader a
    correction in Uzbek.

    Returns `None` when the provider is unreachable or answers unusably, which
    leaves the attempt waiting for a person rather than inventing a verdict.
    """
    gateway = gateway or llm_gateway
    criteria = [row for row in (task.criteria or []) if row.get("key")]
    if not criteria:
        # Nothing to judge against. Assessing anyway would be marking work
        # against a standard nobody wrote down.
        return None

    language = language or next(iter(task.title_i18n or {}), "uz")
    brief = [
        f"TASK: {_text(task.title_i18n, language)}",
        f"WHAT IT ASKS: {_text(task.instructions_i18n, language)}",
        f"WHAT A FINISHED PIECE LOOKS LIKE: {_text(task.outcome_i18n, language)}",
        f"ANSWER LANGUAGE: {language}",
        "",
        "CRITERIA (key | what it asks):",
    ]
    brief += [f"- {row['key']} | {_text(row.get('text_i18n', {}), language)}" for row in criteria]
    brief += ["", "HER SUBMISSION:", _submission_text(attempt)]

    try:
        response = await gateway.complete(
            system=PRACTICE_REVIEW_SYSTEM,
            messages=[{"role": "user", "content": gateway.sanitise("\n".join(brief))}],
            json_schema=PRACTICE_REVIEW_SCHEMA,
            max_tokens=6000,
            effort="low",
        )
    except LlmUnavailableError as exc:
        logger.warning("practice review unavailable: %s", exc)
        return None
    if response.refused or not response.parsed:
        logger.warning("practice review produced no usable verdict")
        return None

    payload = response.parsed
    # Criterion keys are resolved against the task's own list; a key the model
    # invented is dropped, and one it skipped counts as unmet. The verdict is
    # then recomputed from what survived, so "passed" can never rest on a
    # criterion that does not exist.
    known = {row["key"] for row in criteria}
    verdicts = {
        str(row.get("key")): row
        for row in (payload.get("criteria_met") or [])
        if str(row.get("key")) in known
    }
    resolved = [
        {
            "key": row["key"],
            "met": bool(verdicts.get(row["key"], {}).get("met")),
            "note": str(verdicts.get(row["key"], {}).get("note") or "")[:400],
        }
        for row in criteria
    ]
    met = sum(1 for row in resolved if row["met"])
    return {
        "passed": met == len(resolved),
        "score": round(met * 100 / len(resolved), 1),
        "feedback": str(payload.get("feedback") or "").strip()[:4000],
        "criteria_met": resolved,
    }


def _submission_text(attempt: TaskAttempt) -> str:
    """What she handed in, as the reviewer reads it."""
    submission = attempt.submission or {}
    if "text" in submission:
        return str(submission["text"])
    if "link" in submission:
        return f"(she submitted a link to her work: {submission['link']})"
    return "\n\n".join(
        f"[{key}]\n{value}" for key, value in (submission.get("fields") or {}).items()
    )


def evaluator_kind_for(roles: Iterable[Role | str]) -> EvaluatorKind | None:
    """What this account's roles entitle it to sign as, or nothing.

    Read on the server from the token's roles. A woman cannot assess her own
    work whatever her roles say — that check lives at the endpoint, because it
    is about *whose* work, not about who she is.
    """
    held = {getattr(role, "value", role) for role in roles}
    return next((kind for role, kind in ROLE_EVALUATOR if role.value in held), None)


async def record_evaluation(
    session: AsyncSession,
    *,
    attempt: TaskAttempt,
    task: PracticalTask,
    passed: bool,
    score: float | None,
    feedback: str,
    criteria_met: list[dict],
    evaluator_kind: EvaluatorKind,
    evaluator_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> TaskAttempt:
    """Store the verdict, and write what it proves — if it proves anything.

    A pass writes skill evidence of the kind this evaluator's word is worth. A
    needs-improvement writes **nothing**: it is a record of work done, not a
    claim about what she cannot do, and a skill she had already proven survives
    it untouched because nothing was written to weaken it.
    """
    now = now or datetime.now(UTC)
    attempt.passed = passed
    attempt.score = score
    attempt.feedback = feedback
    attempt.criteria_met = criteria_met
    attempt.evaluator_kind = evaluator_kind
    attempt.evaluator_id = evaluator_id
    attempt.evaluated_at = now
    attempt.status = TaskStatus.PASSED if passed else TaskStatus.NEEDS_IMPROVEMENT
    await session.flush()

    if not passed:
        return attempt

    labels = [label for label in (task.skills_practised or []) if label and label.strip()]
    if not labels:
        return attempt

    kind = EVALUATOR_EVIDENCE[evaluator_kind]
    index = await skill_service.SkillIndex.load(session, labels)
    for label in labels:
        # Idempotent on (skill, kind, source): re-evaluating the same attempt
        # updates one row rather than stacking a second claim on the same work.
        await skill_service.record_evidence(
            session,
            user_id=attempt.user_id,
            label=label,
            kind=kind,
            source_type="task_attempt",
            source_id=str(attempt.id),
            level=task.level,
            score=score,
            verified_by_id=evaluator_id,
            occurred_at=now,
            index=index,
        )
    await session.flush()
    return attempt


# ---------------------------------------------------------------------------
# What other features ask
# ---------------------------------------------------------------------------


async def review_queue(session: AsyncSession, *, limit: int = REVIEW_LIMIT) -> list[TaskAttempt]:
    """Submissions waiting for a person, oldest first."""
    rows = await session.execute(
        select(TaskAttempt)
        .where(TaskAttempt.status == TaskStatus.SUBMITTED)
        .order_by(TaskAttempt.submitted_at.asc().nullslast())
        .limit(limit)
    )
    return list(rows.scalars())


async def tasks_for_skills(
    session: AsyncSession, keys: set[str], *, exclude_passed_by: uuid.UUID | None = None
) -> list[PracticalTask]:
    """Published tasks that practise any of a set of taxonomy keys.

    Matched through `SkillIndex` rather than by text, so a task written as
    "Excel" is found for a gap recorded as "эксель".
    """
    if not keys:
        return []
    tasks = await catalogue(session)
    index = await index_for(session, tasks)
    matched = [
        task
        for task in tasks
        if any(index.key(label) in keys for label in (task.skills_practised or []))
    ]
    if exclude_passed_by is None or not matched:
        return matched

    mine = await attempts_for(session, exclude_passed_by, [task.id for task in matched])
    return [
        task
        for task in matched
        if not any(a.status == TaskStatus.PASSED for a in mine.get(task.id, []))
    ]


async def state_for(session: AsyncSession, user_id: uuid.UUID) -> dict[str, list[TaskAttempt]]:
    """Her practice, grouped by where each task stands.

    The shape the AI Coach reads: what is open, what is waiting, what needs
    another go, and what she has passed.
    """
    rows = await session.execute(
        select(TaskAttempt)
        .where(TaskAttempt.user_id == user_id)
        .order_by(TaskAttempt.attempt_no.desc())
    )
    grouped: dict[str, list[TaskAttempt]] = {
        "started": [],
        "submitted": [],
        "needs_improvement": [],
        "passed": [],
    }
    seen: set[uuid.UUID] = set()
    for attempt in rows.scalars():
        # One entry per task: her newest attempt, unless an earlier one passed.
        if attempt.task_id in seen:
            continue
        seen.add(attempt.task_id)
        grouped[attempt.status.value].append(attempt)
    return grouped


def skill_refs(task: PracticalTask, index: skill_service.SkillIndex) -> list[SkillRef]:
    return index.refs(task.skills_practised or [])
