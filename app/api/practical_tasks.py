"""Practical tasks: the catalogue, doing the work, and assessing it.

Thin by design — every decision lives in `services.practice`. What this module
owns is the boundary, and there are three lines drawn here that nothing else
draws:

**Her work is hers.** An attempt is always looked up by the caller's own id.
There is no endpoint that takes a user id, so there is no id to change: asking
for somebody else's submission is not refused, it is unrepresentable.

**She cannot mark her own work.** Evaluating is gated on a role *and* on the
attempt not being her own. A trainer who takes a course is a learner on that
course, and the check is about whose work it is rather than who she is.

**A verdict is only ever written by an evaluation.** No endpoint sets `passed`,
and nothing the browser sends can. The one route into a pass is an assessment
recorded by the AI reviewer or by an authorized person.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import ContentDep, CurrentUserDep, DbSession, OptionalUserDep
from app.core.constants import EvaluatorKind, ProficiencyLevel, Role, TaskStatus, normalise_language
from app.models.practice import PracticalTask, TaskAttempt
from app.models.program import Program
from app.schemas.practice import (
    PracticalTaskCreate,
    PracticalTaskDetail,
    PracticalTaskRead,
    PracticalTaskUpdate,
    ReviewItem,
    TaskEvaluateIn,
    TaskSubmitIn,
)
from app.services import organizations, practice, skills

router = APIRouter(prefix="/practical-tasks", tags=["practical-tasks"])


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PracticalTaskRead])
async def list_tasks(
    session: DbSession,
    user: OptionalUserDep,
    skill: str | None = Query(default=None, max_length=100),
    level: ProficiencyLevel | None = None,
    program_id: uuid.UUID | None = None,
    search: str | None = Query(default=None, max_length=100),
) -> list[PracticalTaskRead]:
    """Published tasks, filtered the way a woman looks for practice.

    Open to visitors, like the programme catalogue it sits beside — a brief is
    public and nothing personal is in it. `skill` resolves through the taxonomy
    rather than matching text, so a gap recorded as "эксель" finds a task
    written as "Excel".
    """
    tasks = await practice.catalogue(session, level=level, program_id=program_id, search=search)
    index = await practice.index_for(session, tasks)

    if skill:
        wanted = index.key(skill)
        tasks = [
            task
            for task in tasks
            if any(index.key(label) == wanted for label in (task.skills_practised or []))
        ]

    programs = await _programs_for(session, tasks)
    if user is None:
        return [
            practice.read(task, index=index, program=programs.get(task.program_id))
            for task in tasks
        ]

    user_id = uuid.UUID(user.id)
    mine = await practice.attempts_for(session, user_id, [task.id for task in tasks])
    held = await skills.skill_keys(session, user_id)
    return [
        practice.read(
            task,
            index=index,
            attempts=mine.get(task.id, []),
            held=held,
            program=programs.get(task.program_id),
        )
        for task in tasks
    ]


@router.get("/me", response_model=list[PracticalTaskDetail])
async def my_tasks(user: CurrentUserDep, session: DbSession) -> list[PracticalTaskDetail]:
    """Every task she has touched, with her own attempts.

    Declared before `/{slug}` so "me" is never read as the name of a task.
    """
    user_id = uuid.UUID(user.id)
    mine = await practice.attempts_for(session, user_id)
    if not mine:
        return []

    rows = await session.execute(select(PracticalTask).where(PracticalTask.id.in_(mine.keys())))
    tasks = list(rows.scalars())
    index = await practice.index_for(session, tasks)
    held = await skills.skill_keys(session, user_id)
    programs = await _programs_for(session, tasks)

    out = [
        practice.detail(
            task,
            index=index,
            attempts=mine.get(task.id, []),
            held=held,
            program=programs.get(task.program_id),
        )
        for task in tasks
    ]
    # What needs her comes first: something to fix, then something to finish,
    # then what is waiting on somebody else, then what is done.
    order = {
        TaskStatus.NEEDS_IMPROVEMENT: 0,
        TaskStatus.STARTED: 1,
        TaskStatus.SUBMITTED: 2,
        TaskStatus.PASSED: 3,
    }
    out.sort(key=lambda row: order.get(row.status, 4) if row.status else 4)
    return out


@router.get("/review", response_model=list[ReviewItem])
async def review_queue(user: CurrentUserDep, session: DbSession) -> list[ReviewItem]:
    """Submissions waiting for a person, oldest first.

    Authorized evaluators only, and it carries the work and the brief it
    answers — not the woman who wrote it. Assessing a budget does not need her
    name, so her name is not sent.
    """
    await _evaluator(user, session)

    attempts = await practice.review_queue(session)
    if not attempts:
        return []

    rows = await session.execute(
        select(PracticalTask).where(PracticalTask.id.in_({attempt.task_id for attempt in attempts}))
    )
    tasks = {task.id: task for task in rows.scalars()}
    index = await practice.index_for(session, tasks.values())
    programs = await _programs_for(session, tasks.values())

    out: list[ReviewItem] = []
    for attempt in attempts:
        task = tasks.get(attempt.task_id)
        if task is None:
            continue
        out.append(
            ReviewItem(
                attempt=practice.attempt_read(attempt, task, index),
                task=practice.detail(task, index=index, program=programs.get(task.program_id)),
            )
        )
    return out


@router.get("/{slug}", response_model=PracticalTaskDetail)
async def read_task(slug: str, session: DbSession, user: OptionalUserDep) -> PracticalTaskDetail:
    """One brief, with her own history of attempting it."""
    task = await _published(session, slug)
    index = await practice.index_for(session, [task])
    program = (await _programs_for(session, [task])).get(task.program_id)

    if user is None:
        return practice.detail(task, index=index, program=program)

    user_id = uuid.UUID(user.id)
    mine = (await practice.attempts_for(session, user_id, [task.id])).get(task.id, [])
    return practice.detail(
        task,
        index=index,
        attempts=mine,
        held=await skills.skill_keys(session, user_id),
        program=program,
    )


# ---------------------------------------------------------------------------
# Doing the work
# ---------------------------------------------------------------------------


@router.post("/{slug}/start", response_model=PracticalTaskDetail)
async def start_task(slug: str, user: CurrentUserDep, session: DbSession) -> PracticalTaskDetail:
    """Open an attempt. Idempotent: one already in progress is returned."""
    task = await _published(session, slug)
    user_id = uuid.UUID(user.id)
    try:
        await practice.start(session, user_id=user_id, task=task)
    except practice.SubmissionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.reason) from exc
    return await _mine(session, task, user_id)


@router.post("/{slug}/submit", response_model=PracticalTaskDetail)
async def submit_task(
    slug: str,
    payload: TaskSubmitIn,
    user: CurrentUserDep,
    session: DbSession,
    language: str | None = None,
) -> PracticalTaskDetail:
    """Hand the work in.

    Validated here whatever the browser believed — a submission arriving
    straight at the API is held to exactly the same brief. On a task the
    platform may assess, the verdict is recorded before this returns; on one it
    may not, the attempt waits for a person and says so.
    """
    task = await _published(session, slug)
    user_id = uuid.UUID(user.id)
    try:
        # Feedback is written in the language she reads the site in. The
        # request's locale wins; her stored one is the fallback.
        await practice.submit(
            session,
            user_id=user_id,
            task=task,
            payload=payload,
            language=normalise_language(language) or user.language.value,
        )
    except practice.SubmissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": exc.reason, "field": exc.field},
        ) from exc
    return await _mine(session, task, user_id)


# ---------------------------------------------------------------------------
# Assessing it
# ---------------------------------------------------------------------------


@router.post("/attempts/{attempt_id}/evaluate", response_model=ReviewItem)
async def evaluate_attempt(
    attempt_id: uuid.UUID,
    payload: TaskEvaluateIn,
    user: CurrentUserDep,
    session: DbSession,
) -> ReviewItem:
    """Record a verdict on somebody else's work.

    Two checks, and they are different questions. The role decides what the
    verdict is *worth* — a mentor or a partner organisation verifies, a trainer
    assesses. Whose work it is decides whether it may be signed at all: nobody
    marks her own, whatever roles she holds.

    What comes back is the one attempt that was assessed, never the learner's
    history with the task. Signing a budget does not entitle anybody to read
    every budget she ever submitted.
    """
    kind, org_id = await _evaluator(user, session)

    attempt = await session.get(TaskAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    if attempt.user_id == uuid.UUID(user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You cannot assess your own work"
        )
    if attempt.status == TaskStatus.STARTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Nothing has been submitted yet"
        )

    task = await session.get(PracticalTask, attempt.task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    await practice.record_evaluation(
        session,
        attempt=attempt,
        task=task,
        passed=payload.passed,
        score=payload.score,
        feedback=payload.feedback,
        criteria_met=[row.model_dump() for row in payload.criteria_met],
        evaluator_kind=kind,
        evaluator_id=uuid.UUID(user.id),
    )
    # Which verified organisation stood behind a partner's verdict. Internal:
    # never shown on a public portfolio.
    attempt.evaluator_org_id = org_id
    index = await practice.index_for(session, [task])
    return ReviewItem(
        attempt=practice.attempt_read(attempt, task, index),
        task=practice.detail(task, index=index),
    )


# ---------------------------------------------------------------------------
# Authoring
# ---------------------------------------------------------------------------


@router.post("", response_model=PracticalTaskDetail, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: PracticalTaskCreate, user: ContentDep, session: DbSession
) -> PracticalTaskDetail:
    """Author a task. Trainers, moderators and admins, as for a programme."""
    exists = await session.scalar(
        select(PracticalTask.id).where(PracticalTask.slug == payload.slug)
    )
    if exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task with slug '{payload.slug}' already exists",
        )

    # `model_dump` already gives plain dicts for the nested models; the columns
    # are JSONB and store them as written.
    task = PracticalTask(
        **payload.model_dump(),
        author_id=uuid.UUID(user.id),
        published_at=datetime.now(UTC) if payload.is_published else None,
    )
    session.add(task)
    await session.flush()
    return practice.detail(task, index=await practice.index_for(session, [task]))


@router.patch("/{task_id}", response_model=PracticalTaskDetail)
async def update_task(
    task_id: uuid.UUID, payload: PracticalTaskUpdate, user: ContentDep, session: DbSession
) -> PracticalTaskDetail:
    task = await session.get(PracticalTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    changes = payload.model_dump(exclude_unset=True)
    if changes.get("criteria") is not None:
        changes["criteria"] = [dict(row) for row in changes["criteria"]]
    if changes.get("is_published") and task.published_at is None:
        task.published_at = datetime.now(UTC)
    for field, value in changes.items():
        setattr(task, field, value)
    await session.flush()
    return practice.detail(task, index=await practice.index_for(session, [task]))


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


async def _published(session: DbSession, slug: str) -> PracticalTask:
    task = await practice.by_slug(session, slug)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


async def _mine(session: DbSession, task: PracticalTask, user_id: uuid.UUID) -> PracticalTaskDetail:
    """The brief with one woman's attempts on it, re-read after a write."""
    mine = (await practice.attempts_for(session, user_id, [task.id])).get(task.id, [])
    return practice.detail(
        task,
        index=await practice.index_for(session, [task]),
        attempts=mine,
        held=await skills.skill_keys(session, user_id),
        program=(await _programs_for(session, [task])).get(task.program_id),
    )


async def _evaluator(user, session: DbSession) -> tuple[EvaluatorKind, uuid.UUID | None]:
    """What this account may sign as — and for which organisation — or a 403.

    Read from the token's roles — both the primary one and the granted list,
    the way `CurrentUser.has_role` reads them. A desk account whose only role
    is the primary would otherwise be refused work it is authorised for.

    A partner's verdict makes a skill *verified*, the strongest claim the
    platform records, so it must be backed by a verified organisation the
    partner acts for (Step 11). Without one, the account signs as whatever
    else it holds — a trainer's assessment — or not at all.
    """
    roles = {user.role, *user.roles}
    kind = practice.evaluator_kind_for(roles)
    if kind == EvaluatorKind.PARTNER:
        org = await organizations.verified_org_for_partner(session, user)
        if org is not None:
            return kind, org.id
        kind = practice.evaluator_kind_for(roles - {Role.PARTNER})
    if kind is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised to assess submissions"
        )
    return kind, None


async def _programs_for(session: DbSession, tasks) -> dict[uuid.UUID, Program]:
    """The courses a set of tasks belong with, in one query."""
    ids = {task.program_id for task in tasks if task.program_id}
    if not ids:
        return {}
    rows = await session.execute(select(Program).where(Program.id.in_(ids)))
    return {program.id: program for program in rows.scalars()}
