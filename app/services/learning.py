"""How far along a course is, and everything that follows finishing one.

One place computes progress, so the learning section and the catalogue can
never disagree about the same enrollment. Progress counts whichever unit the
programme actually has: lessons where they are written, modules where they are
not. That is what lets lessons be added to a course a woman is halfway through
without resetting her.

Finishing a course fans out — the roadmap step that *is* the course closes, the
certificate is issued if the course awards one, and what it taught becomes
skill evidence. All of it runs once, guarded by `completed_at`, so replaying a
completion cannot mint a second certificate or double-count a skill.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import EnrollmentStatus
from app.models.program import Certificate, Enrollment, Program, ProgramLesson, ProgramModule
from app.services import learning_path, skills
from app.services.plan_service import close_plan_items_for_program


async def course_units(session: AsyncSession, program_id: uuid.UUID) -> tuple[list[str], list[str]]:
    """The module ids and lesson ids of a programme, as the strings progress stores."""
    module_ids = list(
        (
            await session.execute(
                select(ProgramModule.id)
                .where(ProgramModule.program_id == program_id)
                .order_by(ProgramModule.order_index)
            )
        ).scalars()
    )
    if not module_ids:
        return [], []

    lesson_ids = list(
        (
            await session.execute(
                select(ProgramLesson.id)
                .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
                .where(ProgramModule.program_id == program_id)
                .order_by(ProgramModule.order_index, ProgramLesson.order_index)
            )
        ).scalars()
    )
    return [str(module_id) for module_id in module_ids], [str(id_) for id_ in lesson_ids]


def _percent(done: int, total: int) -> int:
    return round(done * 100 / total) if total else 0


async def enroll(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    program_id: uuid.UUID,
    now: datetime | None = None,
) -> Enrollment | None:
    """Put her on a course, once. `None` when there is no such published course.

    The one place an enrollment is created, so a course entered from the
    catalogue and the same course entered through a learning path are the same
    row — which is what keeps a second enrollment, and a second progress
    number, from ever existing.
    """
    program = await session.get(Program, program_id)
    if program is None or not program.is_published:
        return None

    existing = await session.scalar(
        select(Enrollment).where(Enrollment.user_id == user_id, Enrollment.program_id == program_id)
    )
    if existing is not None:
        return existing

    now = now or datetime.now(UTC)
    enrollment = Enrollment(
        user_id=user_id,
        program_id=program_id,
        status=EnrollmentStatus.ENROLLED,
        started_at=now,
        last_activity_at=now,
    )
    session.add(enrollment)
    await session.flush()
    return enrollment


async def recompute(
    session: AsyncSession,
    *,
    enrollment: Enrollment,
    program: Program | None = None,
    now: datetime | None = None,
) -> Enrollment:
    """Re-read what she has finished and store where that leaves her.

    Lessons decide when a programme has them; modules when it does not. Ticks
    for units that no longer exist are ignored rather than counted, so deleting
    a module cannot leave someone at 110%.
    """
    now = now or datetime.now(UTC)
    module_ids, lesson_ids = await course_units(session, enrollment.program_id)

    if lesson_ids:
        done = len(set(enrollment.completed_lessons or []) & set(lesson_ids))
        total = len(lesson_ids)
    else:
        done = len(set(enrollment.completed_modules or []) & set(module_ids))
        total = len(module_ids)

    enrollment.progress_percent = _percent(done, total)
    enrollment.last_activity_at = now
    enrollment.status = (
        EnrollmentStatus.COMPLETED if total and done >= total else EnrollmentStatus.IN_PROGRESS
    )

    if enrollment.status == EnrollmentStatus.COMPLETED:
        await complete_course(session, enrollment=enrollment, program=program, now=now)

    await session.flush()
    return enrollment


async def complete_course(
    session: AsyncSession,
    *,
    enrollment: Enrollment,
    program: Program | None = None,
    now: datetime | None = None,
) -> None:
    """Everything that follows finishing a course, exactly once.

    `completed_at` is the guard: a woman who unticks a lesson and ticks it again
    does not collect a second certificate, and a backfill cannot double-record
    what she learned.
    """
    if enrollment.completed_at is not None:
        return

    now = now or datetime.now(UTC)
    enrollment.completed_at = now

    if program is None:
        program = await session.get(Program, enrollment.program_id)
    if program is None:
        return

    # The roadmap step that *is* this course closes itself: the platform just
    # recorded the last lesson, so asking her to confirm it elsewhere is asking
    # her to restate what the system holds.
    await close_plan_items_for_program(session, enrollment.user_id, enrollment.program_id)

    # What the course taught becomes evidence — at the level the course is
    # pitched at, and only ever as *learned*. Nobody has checked what she can
    # do with it yet, and that is what verification means.
    await skills.record_course_completion(
        session,
        user_id=enrollment.user_id,
        labels=program.skills_taught,
        enrollment_id=enrollment.id,
        occurred_at=now,
        level=program.level,
    )

    # A course is a step on any number of routes. Finishing it may be what
    # finishes one of them, and a path can become complete nowhere else — so
    # completion always follows real course completion rather than a button.
    await learning_path.on_program_completed(
        session, user_id=enrollment.user_id, program_id=enrollment.program_id, now=now
    )

    if not program.has_certificate:
        return

    existing = await session.scalar(
        select(Certificate).where(Certificate.enrollment_id == enrollment.id)
    )
    if existing is not None:
        return

    certificate = Certificate(
        enrollment_id=enrollment.id,
        user_id=enrollment.user_id,
        serial_number=f"WU-{now:%Y}-{secrets.token_hex(4).upper()}",
        issued_at=now,
        verification_code=secrets.token_urlsafe(24),
    )
    session.add(certificate)
    await session.flush()
    await skills.record_certificate(
        session,
        user_id=enrollment.user_id,
        labels=program.skills_taught,
        certificate_id=certificate.id,
        occurred_at=now,
    )


def _toggle(ticked: list, unit_id: uuid.UUID, completed: bool) -> list[str]:
    """Ticks are a set of ids; stored sorted so the column reads the same twice."""
    marked = {str(item) for item in ticked or []}
    marked.add(str(unit_id)) if completed else marked.discard(str(unit_id))
    return sorted(marked)


async def mark_lesson(
    session: AsyncSession,
    *,
    enrollment: Enrollment,
    lesson_id: uuid.UUID,
    completed: bool = True,
    program: Program | None = None,
    now: datetime | None = None,
) -> Enrollment:
    """Record that she finished a lesson (or un-finished it) and recompute."""
    enrollment.completed_lessons = _toggle(enrollment.completed_lessons, lesson_id, completed)
    return await recompute(session, enrollment=enrollment, program=program, now=now)


async def mark_module(
    session: AsyncSession,
    *,
    enrollment: Enrollment,
    module_id: uuid.UUID,
    completed: bool = True,
    program: Program | None = None,
    now: datetime | None = None,
) -> Enrollment:
    """The same, for a programme whose modules carry no lessons."""
    enrollment.completed_modules = _toggle(enrollment.completed_modules, module_id, completed)
    return await recompute(session, enrollment=enrollment, program=program, now=now)


async def lesson_in_program(
    session: AsyncSession, *, lesson_id: uuid.UUID, program_id: uuid.UUID
) -> ProgramLesson | None:
    """A lesson, but only if it belongs to the programme she is enrolled in."""
    return await session.scalar(
        select(ProgramLesson)
        .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
        .where(ProgramLesson.id == lesson_id, ProgramModule.program_id == program_id)
    )
