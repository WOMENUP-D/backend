"""The learning endpoints: one course system, and only ever her own progress.

`/talim` reads the same programmes `/dasturlar` sells, so these pin the seams
between them: a course addressed by slug, a lesson with its body, a tick that
persists, and the certificate and skills that finishing produces. Every
personal route answers for the caller and for nobody else.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.constants import (
    EnrollmentStatus,
    Language,
    LessonKind,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
    SkillCategory,
)
from app.models.program import Certificate, Enrollment, Program, ProgramLesson, ProgramModule
from app.models.skill import Skill
from app.models.user import User

PROGRAMS = "/api/v1/programs"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _skill(session, slug: str = "excel") -> Skill:
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
    session,
    *,
    lessons: int = 2,
    published: bool = True,
    certificate: bool = False,
    level: ProficiencyLevel | None = ProficiencyLevel.ELEMENTARY,
) -> Program:
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Kurs", "ru": "Курс", "en": "Course"},
        goal_i18n={"uz": "Maqsad"},
        description_i18n={},
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        level=level,
        learning_outcomes=[],
        skills_taught=["Excel"],
        has_certificate=certificate,
        is_published=published,
    )
    session.add(program)
    await session.flush()

    module = ProgramModule(
        program_id=program.id, order_index=0, title_i18n={"uz": "Modul", "en": "Module"}
    )
    session.add(module)
    await session.flush()
    for index in range(lessons):
        session.add(
            ProgramLesson(
                module_id=module.id,
                order_index=index,
                slug=f"{index + 1}-dars",
                title_i18n={"uz": f"{index + 1}-dars", "en": f"Lesson {index + 1}"},
                kind=LessonKind.READING,
                duration_minutes=20,
                blocks=[{"type": "paragraph", "text": {"uz": "Matn", "en": "Text"}}],
            )
        )
    await session.flush()
    return program


async def _enrol(client, program: Program, headers: dict[str, str]) -> str:
    response = await client.post(f"{PROGRAMS}/{program.id}/enroll", headers=headers)
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.asyncio
async def test_a_course_can_be_opened_by_its_slug_with_its_contents(client, session):
    program = await _course(session, lessons=2)

    response = await client.get(f"{PROGRAMS}/slug/{program.slug}")

    assert response.status_code == 200
    body = response.json()
    assert body["level"] == "elementary"
    [module] = body["modules"]
    assert [lesson["slug"] for lesson in module["lessons"]] == ["1-dars", "2-dars"]
    # The contents list carries no lesson bodies: it is drawn, not read.
    assert "blocks" not in module["lessons"][0]


@pytest.mark.asyncio
async def test_a_draft_course_is_not_readable_by_slug(client, session):
    program = await _course(session, published=False)

    assert (await client.get(f"{PROGRAMS}/slug/{program.slug}")).status_code == 404
    assert (await client.get(f"{PROGRAMS}/slug/nothing-like-this")).status_code == 404


@pytest.mark.asyncio
async def test_a_lesson_is_read_with_its_body(client, session):
    program = await _course(session, lessons=1)

    response = await client.get(f"{PROGRAMS}/{program.id}/lessons/1-dars")

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "reading"
    assert body["blocks"][0]["text"]["uz"] == "Matn"
    assert (await client.get(f"{PROGRAMS}/{program.id}/lessons/no-such-lesson")).status_code == 404


@pytest.mark.asyncio
async def test_enrolling_twice_is_the_same_enrollment(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    program = await _course(session)
    await session.flush()

    first = await client.post(f"{PROGRAMS}/{program.id}/enroll", headers=auth_headers)
    second = await client.post(f"{PROGRAMS}/{program.id}/enroll", headers=auth_headers)

    assert first.json()["id"] == second.json()["id"]
    assert (await client.post(f"{PROGRAMS}/{program.id}/enroll")).status_code == 401


@pytest.mark.asyncio
async def test_a_lesson_tick_persists_and_moves_the_course_on(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    await _skill(session)
    program = await _course(session, lessons=2)
    await session.flush()
    enrollment_id = await _enrol(client, program, auth_headers)
    lessons = list(
        (
            await session.execute(
                select(ProgramLesson)
                .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
                .where(ProgramModule.program_id == program.id)
                .order_by(ProgramLesson.order_index)
            )
        ).scalars()
    )

    done = await client.post(
        f"{PROGRAMS}/enrollments/{enrollment_id}/lessons",
        json={"lesson_id": str(lessons[0].id), "completed": True},
        headers=auth_headers,
    )

    assert done.status_code == 200
    assert done.json()["progress_percent"] == 50
    assert done.json()["completed_lessons"] == [str(lessons[0].id)]

    # Read back through a different route: the tick lives in the database, not
    # in the page she just left.
    [mine] = (await client.get(f"{PROGRAMS}/me/enrollments", headers=auth_headers)).json()
    assert mine["progress_percent"] == 50
    assert mine["program"]["slug"] == program.slug


@pytest.mark.asyncio
async def test_finishing_every_lesson_certifies_her_and_records_the_skill(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    await _skill(session)
    program = await _course(session, lessons=2, certificate=True)
    await session.flush()
    enrollment_id = await _enrol(client, program, auth_headers)
    lessons = list(
        (
            await session.execute(
                select(ProgramLesson)
                .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
                .where(ProgramModule.program_id == program.id)
            )
        ).scalars()
    )

    for lesson in lessons:
        response = await client.post(
            f"{PROGRAMS}/enrollments/{enrollment_id}/lessons",
            json={"lesson_id": str(lesson.id), "completed": True},
            headers=auth_headers,
        )
    assert response.json()["progress_percent"] == 100
    assert response.json()["status"] == "completed"

    certificates = (await client.get(f"{PROGRAMS}/me/certificates", headers=auth_headers)).json()
    assert len(certificates) == 1
    # A certificate that cannot name its course is a serial number.
    assert certificates[0]["program_title_i18n"]["uz"] == "Kurs"

    body = (await client.get("/api/v1/skills/me", headers=auth_headers)).json()
    [excel] = body["skills"]
    assert excel["skill"]["slug"] == "excel"
    assert excel["status"] == "learned"
    assert excel["level"] == "elementary"


@pytest.mark.asyncio
async def test_she_cannot_tick_a_lesson_on_someone_elses_enrollment(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    stranger = User(phone=_phone())
    session.add(stranger)
    program = await _course(session, lessons=1)
    await session.flush()
    hers = Enrollment(
        user_id=stranger.id, program_id=program.id, status=EnrollmentStatus.IN_PROGRESS
    )
    session.add(hers)
    await session.flush()
    lesson = await session.scalar(
        select(ProgramLesson)
        .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
        .where(ProgramModule.program_id == program.id)
    )

    response = await client.post(
        f"{PROGRAMS}/enrollments/{hers.id}/lessons",
        json={"lesson_id": str(lesson.id), "completed": True},
        headers=auth_headers,
    )

    assert response.status_code == 404
    await session.refresh(hers)
    assert hers.completed_lessons == []


@pytest.mark.asyncio
async def test_a_lesson_from_another_course_is_refused(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    mine = await _course(session, lessons=1)
    other = await _course(session, lessons=1)
    await session.flush()
    enrollment_id = await _enrol(client, mine, auth_headers)
    stranger_lesson = await session.scalar(
        select(ProgramLesson)
        .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
        .where(ProgramModule.program_id == other.id)
    )

    response = await client.post(
        f"{PROGRAMS}/enrollments/{enrollment_id}/lessons",
        json={"lesson_id": str(stranger_lesson.id), "completed": True},
        headers=auth_headers,
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_ticking_the_last_lesson_twice_issues_one_certificate(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    await _skill(session)
    program = await _course(session, lessons=1, certificate=True)
    await session.flush()
    enrollment_id = await _enrol(client, program, auth_headers)
    lesson = await session.scalar(
        select(ProgramLesson)
        .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
        .where(ProgramModule.program_id == program.id)
    )

    for completed in (True, False, True):
        await client.post(
            f"{PROGRAMS}/enrollments/{enrollment_id}/lessons",
            json={"lesson_id": str(lesson.id), "completed": completed},
            headers=auth_headers,
        )

    issued = await session.execute(
        select(Certificate).where(Certificate.enrollment_id == uuid.UUID(enrollment_id))
    )
    assert len(list(issued.scalars())) == 1


@pytest.mark.asyncio
async def test_a_module_tick_still_works_for_a_course_without_lessons(
    client, session, auth_headers, user_id
):
    """The seeded catalogue completes this way, and must keep doing so."""
    session.add(User(id=user_id, phone=_phone()))
    await _skill(session)
    program = await _course(session, lessons=0)
    await session.flush()
    enrollment_id = await _enrol(client, program, auth_headers)
    module = await session.scalar(
        select(ProgramModule).where(ProgramModule.program_id == program.id)
    )

    response = await client.post(
        f"{PROGRAMS}/enrollments/{enrollment_id}/progress",
        json={"module_id": str(module.id), "completed": True},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["progress_percent"] == 100
    assert response.json()["status"] == "completed"
