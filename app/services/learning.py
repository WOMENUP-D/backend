"""How far along a course is, and everything that follows finishing one.

One place computes progress, so the learning section and the catalogue can
never disagree about the same enrollment. Progress counts whichever unit the
programme actually has: lessons where they are written, modules where they are
not. That is what lets lessons be added to a course a woman is halfway through
without resetting her.

Finishing a course fans out — the roadmap step that *is* the course closes, the
certificate is issued if the course awards one and she has earned it (a passed
final assessment and a plausible time on the course, `certificate_blockers`),
and what it taught becomes skill evidence. All of it runs once, guarded by
`completed_at`, so replaying a completion cannot mint a second certificate or double-count a skill.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import EnrollmentStatus
from app.models.program import Certificate, Enrollment, Program, ProgramLesson, ProgramModule
from app.services import learning_path, score_progress, skills
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


class ExternalCourseError(Exception):
    """The course lives on somebody else's site: nothing here can track it."""


class TrackedByLessonsError(Exception):
    """A module tick for a programme whose progress is counted in lessons."""


#: The mark a final assessment has to reach before a certificate is issued.
CERTIFICATE_PASS_MARK = 70.0
#: The share of the course's declared study time that has to pass between
#: enrolling and finishing. Four lessons ticked in a tenth of a second are not
#: sixteen hours of study, and a certificate says they were.
CERTIFICATE_MIN_TIME_SHARE = 0.25


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
    if program.external_url:
        # Somebody else's course: the portal links to it and promises nothing
        # about it. An enrollment here could never move past 0 %.
        raise ExternalCourseError(str(program_id))

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

    # Finishing a course is evidence the dimension it teaches has moved.
    await score_progress.on_program_completed(session, user_id=enrollment.user_id, program=program)

    await issue_certificate_if_earned(session, enrollment=enrollment, program=program, now=now)


async def declared_minutes(session: AsyncSession, program_id: uuid.UUID) -> int:
    """The study time the course itself announces: its lessons' minutes, or
    its modules' where it has no lessons."""
    lessons = await session.scalar(
        select(func.coalesce(func.sum(ProgramLesson.duration_minutes), 0))
        .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
        .where(ProgramModule.program_id == program_id)
    )
    if lessons:
        return int(lessons)
    modules = await session.scalar(
        select(func.coalesce(func.sum(ProgramModule.duration_minutes), 0)).where(
            ProgramModule.program_id == program_id
        )
    )
    return int(modules or 0)


def certificate_blockers(enrollment: Enrollment, program: Program, *, minutes: int) -> list[str]:
    """Why this enrollment has not earned a certificate — empty when it has.

    A certificate is a claim to an employer that she studied the course and
    was assessed on it. Ticking every lesson is not that claim: it needs a
    passed final assessment (`final_score`) and a plausible time on the
    course. Until a course has an assessment that writes `final_score`, it
    issues no certificate at all — that is the honest state of the catalogue.
    """
    blockers: list[str] = []
    if not program.has_certificate:
        blockers.append("no_certificate")
    if program.external_url:
        blockers.append("external_course")
    if enrollment.completed_at is None:
        blockers.append("not_completed")
    if enrollment.final_score is None or enrollment.final_score < CERTIFICATE_PASS_MARK:
        blockers.append("assessment_not_passed")
    if minutes and enrollment.completed_at is not None and enrollment.started_at is not None:
        spent = (enrollment.completed_at - enrollment.started_at).total_seconds() / 60
        if spent < minutes * CERTIFICATE_MIN_TIME_SHARE:
            blockers.append("too_fast")
    return blockers


async def issue_certificate_if_earned(
    session: AsyncSession,
    *,
    enrollment: Enrollment,
    program: Program | None = None,
    now: datetime | None = None,
) -> Certificate | None:
    """Issue the certificate once every condition holds; idempotent.

    Called when the course is completed, and to be called again by whatever
    records a final assessment, since that may come after the last lesson.
    """
    if program is None:
        program = await session.get(Program, enrollment.program_id)
    if program is None:
        return None
    minutes = await declared_minutes(session, program.id)
    if certificate_blockers(enrollment, program, minutes=minutes):
        return None

    existing = await session.scalar(
        select(Certificate).where(Certificate.enrollment_id == enrollment.id)
    )
    if existing is not None:
        return existing

    now = now or datetime.now(UTC)
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
    return certificate


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
    """The same, for a programme whose modules carry no lessons.

    A programme with lessons counts lessons; a module tick there would be
    stored and then ignored, so it is refused instead of reported as done.
    """
    _, lesson_ids = await course_units(session, enrollment.program_id)
    if lesson_ids:
        raise TrackedByLessonsError(str(enrollment.program_id))
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
