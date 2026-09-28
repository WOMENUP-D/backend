"""Learning paths: a route through courses that already exist.

These pin the promises the feature makes. A path owns no content, so the tests
check the seams rather than the surface: that the order is the backend's and
not the button's, that progress is her enrollments read back rather than a
number a path keeps, that finishing the last required course is what finishes
the route, and that the skills it leaves her with are *learned* and never
verified.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.constants import (
    EvidenceKind,
    Language,
    LessonKind,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
    ScoreDimension,
    SkillCategory,
    SkillStatus,
)
from app.models.learning_path import LearningPath, LearningPathItem, UserLearningPath
from app.models.program import Enrollment, Program, ProgramLesson, ProgramModule
from app.models.skill import Skill, SkillEvidence, UserSkill
from app.models.user import User

PATHS = "/api/v1/learning-paths"
PROGRAMS = "/api/v1/programs"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _skill(session, slug: str, label: str, *aliases: str) -> Skill:
    """A curated taxonomy entry. Extra aliases are the spellings found in the
    wild — the Russian one is how a Cyrillic label folds onto a Latin slug."""
    skill = Skill(
        slug=slug,
        name_i18n={"uz": label, "ru": label, "en": label},
        category=SkillCategory.FINANCE,
        dimensions=[],
        aliases=[slug, label.lower(), *aliases],
        is_curated=True,
    )
    session.add(skill)
    await session.flush()
    return skill


async def _course(session, *, skills: list[str], lessons: int = 1, hours: float = 10.0) -> Program:
    """A published course with one module and `lessons` lessons under it."""
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Kurs", "ru": "Курс", "en": "Course"},
        goal_i18n={"uz": "Maqsad"},
        description_i18n={},
        category=ProgramCategory.FINANCIAL_LITERACY,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        level=ProficiencyLevel.BEGINNER,
        duration_hours=hours,
        duration_weeks=2,
        learning_outcomes=[],
        skills_taught=skills,
        has_certificate=False,
        is_published=True,
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
                blocks=[],
            )
        )
    await session.flush()
    return program


async def _path(
    session,
    programs: list[Program],
    *,
    slug: str | None = None,
    required: list[bool] | None = None,
    published: bool = True,
    level: ProficiencyLevel | None = ProficiencyLevel.BEGINNER,
    dimension: ScoreDimension | None = ScoreDimension.FINANCIAL_LITERACY,
) -> LearningPath:
    path = LearningPath(
        slug=slug or f"path-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Yoʻnalish", "ru": "Путь", "en": "Path"},
        description_i18n={"uz": "Tavsif", "ru": "Описание", "en": "Description"},
        dimension=dimension,
        level=level,
        is_published=published,
    )
    session.add(path)
    await session.flush()
    flags = required or [True] * len(programs)
    for index, (program, is_required) in enumerate(zip(programs, flags, strict=True)):
        session.add(
            LearningPathItem(
                path_id=path.id,
                program_id=program.id,
                order_index=index,
                is_required=is_required,
            )
        )
    await session.flush()
    await session.refresh(path, ["items"])
    return path


async def _finish(client, session, program: Program, headers: dict) -> None:
    """Take a course to 100% the way she would: enrol, then tick every lesson."""
    enrollment_id = (await client.post(f"{PROGRAMS}/{program.id}/enroll", headers=headers)).json()[
        "id"
    ]
    lessons = list(
        (
            await session.execute(
                select(ProgramLesson.id)
                .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
                .where(ProgramModule.program_id == program.id)
            )
        ).scalars()
    )
    for lesson_id in lessons:
        response = await client.post(
            f"{PROGRAMS}/enrollments/{enrollment_id}/lessons",
            json={"lesson_id": str(lesson_id), "completed": True},
            headers=headers,
        )
        assert response.status_code == 200


# --- the catalogue ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_catalogue_lists_published_paths_with_what_they_teach(client, session):
    await _skill(session, "budjet", "Byudjet")
    first = await _course(session, skills=["Byudjet"], hours=16)
    second = await _course(session, skills=["Kredit"], hours=20)
    await _path(session, [first, second], slug="pul-yoli")
    await _path(session, [first], published=False)

    response = await client.get(PATHS)

    assert response.status_code == 200
    [path] = response.json()
    assert path["slug"] == "pul-yoli"
    assert path["program_count"] == 2
    assert path["required_count"] == 2
    # Hours and skills are read off the courses, never stored on the path.
    assert path["total_hours"] == 36.0
    assert path["total_weeks"] == 4
    assert [skill["label"] for skill in path["skills"]] == ["Byudjet", "Kredit"]
    # A curated skill comes back with its taxonomy slug and its three names.
    assert path["skills"][0]["slug"] == "budjet"
    assert path["skills"][0]["name_i18n"]["ru"] == "Byudjet"
    # A visitor has no progress, and is not told she has none of something.
    assert path["progress"]["status"] == "not_started"
    assert path["progress"]["percent"] == 0


@pytest.mark.asyncio
async def test_the_catalogue_is_filtered_by_dimension_level_and_skill(client, session):
    await _skill(session, "buxgalteriya", "Buxgalteriya", "бухгалтерия")
    money = await _course(session, skills=["Byudjet"])
    books = await _course(session, skills=["бухгалтерия"])
    await _path(session, [money], slug="pul", dimension=ScoreDimension.FINANCIAL_LITERACY)
    await _path(
        session,
        [books],
        slug="ish",
        dimension=ScoreDimension.EMPLOYMENT,
        level=ProficiencyLevel.INTERMEDIATE,
    )

    by_dimension = await client.get(f"{PATHS}?dimension=employment")
    by_level = await client.get(f"{PATHS}?level=intermediate")
    # Typed in Latin, written in the catalogue in Cyrillic: the same skill.
    by_skill = await client.get(f"{PATHS}?skill=buxgalteriya")
    by_search = await client.get(f"{PATHS}?search=pul")

    assert [row["slug"] for row in by_dimension.json()] == ["ish"]
    assert [row["slug"] for row in by_level.json()] == ["ish"]
    assert [row["slug"] for row in by_skill.json()] == ["ish"]
    assert [row["slug"] for row in by_search.json()] == ["pul"]
    assert (await client.get(f"{PATHS}?skill=nothing-teaches-this")).json() == []


@pytest.mark.asyncio
async def test_a_path_is_read_by_slug_with_its_steps_in_order(client, session):
    first = await _course(session, skills=["Byudjet"])
    second = await _course(session, skills=["Kredit"])
    path = await _path(session, [second, first], slug="tartib")
    # Stored out of order on purpose: the order_index decides, not the insert.
    items = list(
        (
            await session.execute(
                select(LearningPathItem).where(LearningPathItem.path_id == path.id)
            )
        ).scalars()
    )
    for item in items:
        item.order_index = 0 if item.program_id == second.id else 1
    await session.flush()

    response = await client.get(f"{PATHS}/tartib")

    assert response.status_code == 200
    body = response.json()
    assert body["description_i18n"]["ru"] == "Описание"
    assert [item["program"]["id"] for item in body["items"]] == [str(second.id), str(first.id)]
    # Nobody signed in: the first step is open and the rest are honest about it.
    assert [item["status"] for item in body["items"]] == ["available", "locked"]
    assert (await client.get(f"{PATHS}/no-such-path")).status_code == 404


@pytest.mark.asyncio
async def test_a_draft_path_is_not_readable(client, session):
    program = await _course(session, skills=["Byudjet"])
    await _path(session, [program], slug="qoralama", published=False)

    assert (await client.get(f"{PATHS}/qoralama")).status_code == 404


# --- starting one -----------------------------------------------------------


@pytest.mark.asyncio
async def test_starting_a_path_needs_an_account(client, session):
    program = await _course(session, skills=["Byudjet"])
    await _path(session, [program], slug="yol")

    assert (await client.post(f"{PATHS}/yol/start")).status_code == 401
    assert (await client.get(f"{PATHS}/me")).status_code == 401


@pytest.mark.asyncio
async def test_starting_a_path_twice_records_it_once_and_enrols_in_nothing(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    program = await _course(session, skills=["Byudjet"])
    await _path(session, [program], slug="yol")
    await session.flush()

    first = await client.post(f"{PATHS}/yol/start", headers=auth_headers)
    second = await client.post(f"{PATHS}/yol/start", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    records = list(
        (
            await session.execute(
                select(UserLearningPath).where(UserLearningPath.user_id == user_id)
            )
        ).scalars()
    )
    assert len(records) == 1
    # Taking a path on is a decision about direction, not five enrollments.
    enrollments = list(
        (await session.execute(select(Enrollment).where(Enrollment.user_id == user_id))).scalars()
    )
    assert enrollments == []


@pytest.mark.asyncio
async def test_my_paths_answers_for_the_caller_only(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    stranger = uuid.uuid4()
    session.add(User(id=stranger, phone=_phone()))
    program = await _course(session, skills=["Byudjet"])
    path = await _path(session, [program], slug="yol")
    await session.flush()
    session.add(UserLearningPath(user_id=stranger, path_id=path.id, started_at=datetime.now(UTC)))
    await session.flush()

    response = await client.get(f"{PATHS}/me", headers=auth_headers)

    # Somebody else started it; that is not her record.
    assert response.json() == []


# --- prerequisites ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_locked_step_is_refused_by_the_backend(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    first = await _course(session, skills=["Byudjet"])
    second = await _course(session, skills=["Kredit"])
    await _path(session, [first, second], slug="ketma-ket")
    await session.flush()

    blocked = await client.post(
        f"{PATHS}/ketma-ket/programs/{second.id}/enroll", headers=auth_headers
    )

    assert blocked.status_code == 409
    assert (
        await client.post(f"{PATHS}/ketma-ket/programs/{first.id}/enroll", headers=auth_headers)
    ).status_code == 201


@pytest.mark.asyncio
async def test_finishing_a_step_unlocks_the_next_one(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    first = await _course(session, skills=["Byudjet"], lessons=1)
    second = await _course(session, skills=["Kredit"])
    await _path(session, [first, second], slug="ketma-ket")
    await session.flush()

    await _finish(client, session, first, auth_headers)
    opened = await client.post(
        f"{PATHS}/ketma-ket/programs/{second.id}/enroll", headers=auth_headers
    )

    assert opened.status_code == 201
    body = (await client.get(f"{PATHS}/ketma-ket", headers=auth_headers)).json()
    assert [item["status"] for item in body["items"]] == ["completed", "in_progress"]


@pytest.mark.asyncio
async def test_an_optional_step_never_blocks_the_one_after_it(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    first = await _course(session, skills=["Byudjet"], lessons=1)
    extra = await _course(session, skills=["Qoʻshimcha"])
    last = await _course(session, skills=["Kredit"])
    await _path(session, [first, extra, last], slug="ixtiyoriy", required=[True, False, True])
    await session.flush()

    await _finish(client, session, first, auth_headers)

    body = (await client.get(f"{PATHS}/ixtiyoriy", headers=auth_headers)).json()
    assert [item["status"] for item in body["items"]] == ["completed", "available", "available"]
    # The optional step is not in the denominator: one of two required is 50%.
    assert body["progress"]["percent"] == 50
    assert body["progress"]["required_items"] == 2
    assert body["progress"]["total_items"] == 3


@pytest.mark.asyncio
async def test_a_course_she_already_started_is_never_locked(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    first = await _course(session, skills=["Byudjet"])
    second = await _course(session, skills=["Kredit"])
    await _path(session, [first, second], slug="katalogdan")
    await session.flush()

    # She found the second course in the open catalogue and enrolled there.
    enrolled = await client.post(f"{PROGRAMS}/{second.id}/enroll", headers=auth_headers)
    assert enrolled.status_code == 201

    body = (await client.get(f"{PATHS}/katalogdan", headers=auth_headers)).json()
    assert [item["status"] for item in body["items"]] == ["available", "in_progress"]
    # And a path may not take it away from her again.
    again = await client.post(
        f"{PATHS}/katalogdan/programs/{second.id}/enroll", headers=auth_headers
    )
    assert again.status_code == 201
    assert again.json()["id"] == enrolled.json()["id"]


@pytest.mark.asyncio
async def test_a_programme_outside_the_path_is_not_enrollable_through_it(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    inside = await _course(session, skills=["Byudjet"])
    outside = await _course(session, skills=["Kredit"])
    await _path(session, [inside], slug="yol")
    await session.flush()

    response = await client.post(f"{PATHS}/yol/programs/{outside.id}/enroll", headers=auth_headers)

    assert response.status_code == 404


# --- progress and completion ------------------------------------------------


@pytest.mark.asyncio
async def test_path_progress_is_her_enrollments_read_back(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    first = await _course(session, skills=["Byudjet"], lessons=1)
    second = await _course(session, skills=["Kredit"], lessons=2)
    await _path(session, [first, second], slug="yol")
    await session.flush()
    await client.post(f"{PATHS}/yol/start", headers=auth_headers)

    await _finish(client, session, first, auth_headers)
    await client.post(f"{PATHS}/yol/programs/{second.id}/enroll", headers=auth_headers)

    body = (await client.get(f"{PATHS}/yol", headers=auth_headers)).json()
    progress = body["progress"]
    assert progress["status"] == "in_progress"
    assert progress["percent"] == 50
    assert progress["completed_items"] == 1
    assert progress["current_program_id"] == str(second.id)
    assert progress["next_program_id"] is None
    assert progress["started_at"] is not None
    assert progress["completed_at"] is None
    # The step's own figure is the enrollment's, not a second count.
    assert body["items"][1]["progress_percent"] == 0
    assert body["items"][1]["enrollment_id"] is not None


@pytest.mark.asyncio
async def test_finishing_every_required_course_completes_the_path_and_teaches_its_skills(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    await _skill(session, "budjet", "Byudjet")
    await _skill(session, "kredit", "Kredit")
    first = await _course(session, skills=["Byudjet"], lessons=1)
    second = await _course(session, skills=["Kredit"], lessons=1)
    extra = await _course(session, skills=["Qoʻshimcha"], lessons=1)
    path = await _path(session, [first, second, extra], slug="yol", required=[True, True, False])
    await session.flush()
    await client.post(f"{PATHS}/yol/start", headers=auth_headers)

    await _finish(client, session, first, auth_headers)
    await _finish(client, session, second, auth_headers)

    body = (await client.get(f"{PATHS}/yol", headers=auth_headers)).json()
    assert body["progress"]["status"] == "completed"
    assert body["progress"]["percent"] == 100
    assert body["progress"]["completed_at"] is not None
    # The optional step is still open — finishing the route did not close it.
    assert body["items"][2]["status"] == "available"

    # It taught her something, and what it taught is *learned*. A route through
    # courses is teaching; nobody outside the platform has checked her yet.
    evidence = list(
        (
            await session.execute(
                select(SkillEvidence)
                .join(UserSkill, UserSkill.id == SkillEvidence.user_skill_id)
                .where(
                    UserSkill.user_id == user_id,
                    SkillEvidence.kind == EvidenceKind.LEARNING_PATH,
                )
            )
        ).scalars()
    )
    assert {item.source_id for item in evidence} == {str(path.id)}
    assert len(evidence) == 2  # required steps only
    assert all(item.level == ProficiencyLevel.BEGINNER for item in evidence)
    statuses = list(
        (
            await session.execute(select(UserSkill.status).where(UserSkill.user_id == user_id))
        ).scalars()
    )
    assert set(statuses) == {SkillStatus.LEARNED}


@pytest.mark.asyncio
async def test_a_path_completes_once_however_often_the_last_lesson_is_reticked(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    await _skill(session, "budjet", "Byudjet")
    program = await _course(session, skills=["Byudjet"], lessons=1)
    path = await _path(session, [program], slug="yol")
    await session.flush()
    await client.post(f"{PATHS}/yol/start", headers=auth_headers)

    await _finish(client, session, program, auth_headers)
    record = await session.scalar(
        select(UserLearningPath).where(UserLearningPath.path_id == path.id)
    )
    stamped = record.completed_at
    await _finish(client, session, program, auth_headers)

    await session.refresh(record)
    assert record.completed_at == stamped
    evidence = list(
        (
            await session.execute(
                select(SkillEvidence).where(SkillEvidence.kind == EvidenceKind.LEARNING_PATH)
            )
        ).scalars()
    )
    assert len(evidence) == 1


@pytest.mark.asyncio
async def test_a_path_she_never_started_is_not_awarded_to_her(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    await _skill(session, "budjet", "Byudjet")
    program = await _course(session, skills=["Byudjet"], lessons=1)
    path = await _path(session, [program], slug="yol")
    await session.flush()

    await _finish(client, session, program, auth_headers)

    record = await session.scalar(
        select(UserLearningPath).where(UserLearningPath.path_id == path.id)
    )
    assert record is None
    assert (
        await session.scalar(
            select(SkillEvidence).where(SkillEvidence.kind == EvidenceKind.LEARNING_PATH)
        )
    ) is None

    # But it is still hers to claim, and claiming it closes it at once.
    started = await client.post(f"{PATHS}/yol/start", headers=auth_headers)
    assert started.json()["progress"]["status"] == "completed"
    assert started.json()["progress"]["completed_at"] is not None


@pytest.mark.asyncio
async def test_my_paths_shows_routes_her_enrollments_already_put_her_on(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    first = await _course(session, skills=["Byudjet"], lessons=1)
    second = await _course(session, skills=["Kredit"])
    await _path(session, [first, second], slug="yol")
    await session.flush()

    # She never pressed "start" — she enrolled in the first course from the
    # catalogue. The progress is real, so the path is real.
    await client.post(f"{PROGRAMS}/{first.id}/enroll", headers=auth_headers)

    response = await client.get(f"{PATHS}/me", headers=auth_headers)

    [path] = response.json()
    assert path["slug"] == "yol"
    assert path["progress"]["status"] == "in_progress"
    assert path["progress"]["percent"] == 0
    # Started is a fact she has not stated, so it is not claimed for her.
    assert path["progress"]["started_at"] is None
    assert path["items"][0]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_a_path_names_the_skills_she_does_not_hold_yet(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    await _skill(session, "budjet", "Byudjet")
    await _skill(session, "kredit", "Kredit")
    first = await _course(session, skills=["Byudjet"], lessons=1)
    second = await _course(session, skills=["Kredit"])
    await _path(session, [first, second], slug="yol")
    await session.flush()

    await _finish(client, session, first, auth_headers)

    [path] = (await client.get(PATHS, headers=auth_headers)).json()
    assert [skill["label"] for skill in path["skills"]] == ["Byudjet", "Kredit"]
    # She learned the first one by finishing it; only the second is still new.
    assert [skill["label"] for skill in path["new_skills"]] == ["Kredit"]
