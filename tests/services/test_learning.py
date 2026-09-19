"""Progress, completion, and everything that follows finishing a course.

One place computes progress so the learning section and the catalogue cannot
disagree. What these pin down: lessons decide progress where they exist and
modules where they do not, finishing runs its consequences exactly once, and a
completion writes *learned* skill evidence at the level the course teaches —
never verified.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    EnrollmentStatus,
    EvidenceKind,
    Language,
    LessonKind,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
    SkillCategory,
    SkillStatus,
)
from app.models.program import Certificate, Enrollment, Program, ProgramLesson, ProgramModule
from app.models.skill import Skill, SkillEvidence, UserSkill
from app.models.user import User
from app.seed_lessons import carry_progress_over
from app.services import learning, skills


async def _user(session: AsyncSession) -> uuid.UUID:
    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}")
    session.add(user)
    await session.flush()
    return user.id


async def _skill(session: AsyncSession, slug: str = "excel") -> Skill:
    skill = Skill(
        slug=slug,
        name_i18n={"uz": "Excel", "ru": "Excel", "en": "Excel"},
        category=SkillCategory.DIGITAL,
        dimensions=[],
        aliases=[slug],
        is_curated=True,
    )
    session.add(skill)
    await session.flush()
    return skill


async def _course(
    session: AsyncSession,
    *,
    modules: int = 2,
    lessons_per_module: int = 0,
    certificate: bool = False,
    level: ProficiencyLevel | None = None,
    taught: tuple[str, ...] = ("Excel",),
) -> Program:
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Kurs", "ru": "Курс", "en": "Course"},
        goal_i18n={},
        description_i18n={},
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        level=level,
        learning_outcomes=[],
        skills_taught=list(taught),
        has_certificate=certificate,
        is_published=True,
    )
    session.add(program)
    await session.flush()

    for module_index in range(modules):
        module = ProgramModule(
            program_id=program.id,
            order_index=module_index,
            title_i18n={"uz": f"{module_index + 1}-modul"},
        )
        session.add(module)
        await session.flush()
        for lesson_index in range(lessons_per_module):
            session.add(
                ProgramLesson(
                    module_id=module.id,
                    order_index=lesson_index,
                    slug=f"{module_index + 1}-{lesson_index + 1}-dars",
                    title_i18n={"uz": "Dars"},
                    kind=LessonKind.READING,
                    duration_minutes=20,
                    blocks=[{"type": "paragraph", "text": {"uz": "Matn"}}],
                )
            )
    await session.flush()
    return program


async def _enrol(session: AsyncSession, user_id: uuid.UUID, program: Program) -> Enrollment:
    enrollment = Enrollment(
        user_id=user_id,
        program_id=program.id,
        status=EnrollmentStatus.ENROLLED,
        started_at=datetime.now(UTC),
    )
    session.add(enrollment)
    await session.flush()
    return enrollment


async def _lessons(session: AsyncSession, program: Program) -> list[ProgramLesson]:
    rows = await session.execute(
        select(ProgramLesson)
        .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
        .where(ProgramModule.program_id == program.id)
        .order_by(ProgramModule.order_index, ProgramLesson.order_index)
    )
    return list(rows.scalars())


@pytest.mark.asyncio
async def test_progress_counts_lessons_when_a_course_has_them(session):
    user_id = await _user(session)
    program = await _course(session, modules=2, lessons_per_module=2)
    enrollment = await _enrol(session, user_id, program)
    lessons = await _lessons(session, program)

    assert enrollment.progress_percent == 0

    await learning.mark_lesson(session, enrollment=enrollment, lesson_id=lessons[0].id)
    assert enrollment.progress_percent == 25
    assert enrollment.status is EnrollmentStatus.IN_PROGRESS

    await learning.mark_lesson(session, enrollment=enrollment, lesson_id=lessons[1].id)
    await learning.mark_lesson(session, enrollment=enrollment, lesson_id=lessons[2].id)
    assert enrollment.progress_percent == 75
    assert enrollment.completed_at is None


@pytest.mark.asyncio
async def test_a_course_without_lessons_still_counts_modules(session):
    """The whole seeded catalogue works this way until lessons are written."""
    user_id = await _user(session)
    program = await _course(session, modules=4, lessons_per_module=0)
    enrollment = await _enrol(session, user_id, program)
    module_ids, lesson_ids = await learning.course_units(session, program.id)
    assert lesson_ids == []

    await learning.mark_module(session, enrollment=enrollment, module_id=uuid.UUID(module_ids[0]))

    assert enrollment.progress_percent == 25


@pytest.mark.asyncio
async def test_unticking_a_lesson_takes_the_progress_back(session):
    user_id = await _user(session)
    program = await _course(session, modules=1, lessons_per_module=2)
    enrollment = await _enrol(session, user_id, program)
    lessons = await _lessons(session, program)

    await learning.mark_lesson(session, enrollment=enrollment, lesson_id=lessons[0].id)
    await learning.mark_lesson(
        session, enrollment=enrollment, lesson_id=lessons[0].id, completed=False
    )

    assert enrollment.progress_percent == 0
    assert enrollment.completed_lessons == []


@pytest.mark.asyncio
async def test_finishing_every_lesson_completes_the_course_and_issues_the_certificate(session):
    user_id = await _user(session)
    await _skill(session)
    program = await _course(
        session,
        modules=1,
        lessons_per_module=2,
        certificate=True,
        level=ProficiencyLevel.INTERMEDIATE,
    )
    enrollment = await _enrol(session, user_id, program)

    for lesson in await _lessons(session, program):
        await learning.mark_lesson(session, enrollment=enrollment, lesson_id=lesson.id)

    assert enrollment.progress_percent == 100
    assert enrollment.status is EnrollmentStatus.COMPLETED
    assert enrollment.completed_at is not None

    certificates = await session.execute(
        select(Certificate).where(Certificate.enrollment_id == enrollment.id)
    )
    assert len(list(certificates.scalars())) == 1


@pytest.mark.asyncio
async def test_completion_writes_learned_evidence_at_the_courses_own_level(session):
    """Not every course is a beginner course, and the evidence should say so."""
    user_id = await _user(session)
    await _skill(session)
    program = await _course(
        session, modules=1, lessons_per_module=1, level=ProficiencyLevel.ADVANCED
    )
    enrollment = await _enrol(session, user_id, program)

    [lesson] = await _lessons(session, program)
    await learning.mark_lesson(session, enrollment=enrollment, lesson_id=lesson.id)

    state = await session.scalar(
        select(UserSkill)
        .join(Skill, Skill.id == UserSkill.skill_id)
        .where(UserSkill.user_id == user_id, Skill.slug == "excel")
    )
    assert state.level is ProficiencyLevel.ADVANCED
    # Taught at an advanced level, and still only *learned*: nobody has checked
    # what she can do with it.
    assert state.status is SkillStatus.LEARNED


@pytest.mark.asyncio
async def test_an_unclassified_course_falls_back_to_its_own_floor(session):
    user_id = await _user(session)
    await _skill(session)
    program = await _course(session, modules=1, lessons_per_module=1, level=None)
    enrollment = await _enrol(session, user_id, program)

    [lesson] = await _lessons(session, program)
    await learning.mark_lesson(session, enrollment=enrollment, lesson_id=lesson.id)

    state = await session.scalar(
        select(UserSkill)
        .join(Skill, Skill.id == UserSkill.skill_id)
        .where(UserSkill.user_id == user_id, Skill.slug == "excel")
    )
    assert state.level is ProficiencyLevel.BEGINNER


@pytest.mark.asyncio
async def test_finishing_twice_costs_nothing_extra(session):
    """Unticking and re-ticking the last lesson must not mint a second
    certificate or count the skill twice."""
    user_id = await _user(session)
    await _skill(session)
    program = await _course(session, modules=1, lessons_per_module=1, certificate=True)
    enrollment = await _enrol(session, user_id, program)
    [lesson] = await _lessons(session, program)

    for completed in (True, False, True, True):
        await learning.mark_lesson(
            session, enrollment=enrollment, lesson_id=lesson.id, completed=completed
        )

    certificates = await session.execute(
        select(Certificate).where(Certificate.enrollment_id == enrollment.id)
    )
    assert len(list(certificates.scalars())) == 1

    evidence = await session.execute(
        select(SkillEvidence)
        .join(UserSkill, UserSkill.id == SkillEvidence.user_skill_id)
        .where(
            UserSkill.user_id == user_id,
            SkillEvidence.kind == EvidenceKind.COURSE_COMPLETION,
        )
    )
    assert len(list(evidence.scalars())) == 1


@pytest.mark.asyncio
async def test_a_lesson_from_another_course_is_not_hers_to_tick(session):
    mine = await _course(session, modules=1, lessons_per_module=1)
    other = await _course(session, modules=1, lessons_per_module=1)
    [stranger_lesson] = await _lessons(session, other)

    found = await learning.lesson_in_program(
        session, lesson_id=stranger_lesson.id, program_id=mine.id
    )

    assert found is None


@pytest.mark.asyncio
async def test_ticks_for_lessons_that_no_longer_exist_are_ignored(session):
    """Deleting a lesson must not leave someone stuck above a hundred per cent."""
    user_id = await _user(session)
    program = await _course(session, modules=1, lessons_per_module=2)
    enrollment = await _enrol(session, user_id, program)
    lessons = await _lessons(session, program)

    await learning.mark_lesson(session, enrollment=enrollment, lesson_id=lessons[0].id)
    enrollment.completed_lessons = [*enrollment.completed_lessons, str(uuid.uuid4())]
    await learning.recompute(session, enrollment=enrollment)

    assert enrollment.progress_percent == 50


@pytest.mark.asyncio
async def test_a_course_recorded_as_finished_has_its_lessons_finished(session):
    """Older records carry a progress number and no unit-level ticks at all.

    "Completed" already says every lesson is behind her, so the ticks follow
    from the status rather than being guessed — and the percentage is then
    recomputed rather than left at whatever was stored.
    """
    user_id = await _user(session)
    await _skill(session)
    program = await _course(session, modules=2, lessons_per_module=1)
    enrollment = Enrollment(
        user_id=user_id,
        program_id=program.id,
        status=EnrollmentStatus.COMPLETED,
        progress_percent=84,
        started_at=datetime.now(UTC),
    )
    session.add(enrollment)
    await session.flush()

    moved = await carry_progress_over(session)

    assert moved == 1
    assert enrollment.progress_percent == 100
    assert len(enrollment.completed_lessons) == 2


@pytest.mark.asyncio
async def test_a_course_still_in_progress_is_left_alone(session):
    """Which lessons she got through is not ours to guess."""
    user_id = await _user(session)
    program = await _course(session, modules=2, lessons_per_module=1)
    enrollment = Enrollment(
        user_id=user_id,
        program_id=program.id,
        status=EnrollmentStatus.IN_PROGRESS,
        progress_percent=33,
        started_at=datetime.now(UTC),
    )
    session.add(enrollment)
    await session.flush()

    assert await carry_progress_over(session) == 0
    assert enrollment.completed_lessons == []
    assert enrollment.progress_percent == 33


@pytest.mark.asyncio
async def test_finished_modules_carry_over_to_the_lessons_they_became(session):
    """She finished the module before lessons existed, and the lesson is that
    same text — so it is finished too."""
    user_id = await _user(session)
    program = await _course(session, modules=2, lessons_per_module=0)
    enrollment = await _enrol(session, user_id, program)
    module_ids, _ = await learning.course_units(session, program.id)
    await learning.mark_module(session, enrollment=enrollment, module_id=uuid.UUID(module_ids[0]))

    # Lessons are written for the course afterwards.
    for index, module_id in enumerate(module_ids):
        session.add(
            ProgramLesson(
                module_id=uuid.UUID(module_id),
                order_index=0,
                slug=f"{index + 1}-dars",
                title_i18n={"uz": "Dars"},
            )
        )
    await session.flush()

    moved = await carry_progress_over(session)
    await learning.recompute(session, enrollment=enrollment)

    assert moved == 1
    # Half the course, counted in lessons now, and the same half she had.
    assert enrollment.progress_percent == 50


@pytest.mark.asyncio
async def test_adding_lessons_later_does_not_reset_her(session):
    """A course she finished at module granularity stays finished."""
    user_id = await _user(session)
    await _skill(session)
    program = await _course(session, modules=1, lessons_per_module=0)
    enrollment = await _enrol(session, user_id, program)
    module_ids, _ = await learning.course_units(session, program.id)

    await learning.mark_module(session, enrollment=enrollment, module_id=uuid.UUID(module_ids[0]))
    assert enrollment.status is EnrollmentStatus.COMPLETED
    completed_at = enrollment.completed_at

    session.add(
        ProgramLesson(
            module_id=uuid.UUID(module_ids[0]),
            order_index=0,
            slug="new-lesson",
            title_i18n={"uz": "Yangi dars"},
        )
    )
    await session.flush()
    await learning.recompute(session, enrollment=enrollment)

    # She is measured against the new shape of the course, but what she already
    # earned — the completion date, the certificate, the evidence — stands.
    assert enrollment.progress_percent == 0
    assert enrollment.completed_at == completed_at
    assert await skills.skill_keys(session, user_id) == {"excel"}
