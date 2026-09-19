"""Career paths: a direction toward work, read against records that exist.

What is pinned here, in order of how badly it would hurt to get wrong:

* Every course, task and listing a direction names is a real, live record —
  published, open and of the right kind. Nothing is invented to fill a stage.
* Her journey is hers alone: there is no route that reads another woman's, a
  visitor sees no personal state, and choosing a direction enrols, starts and
  applies to nothing.
* Stages are read from her records — enrollments, skill evidence, task
  attempts, portfolio projects, applications — and never ticked. A skill she
  only listed herself does not finish "Learn".
* Listings are not put in front of a girl the platform knows to be under 18.
* The Development Score is read, never changed.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.constants import (
    ApplicationStatus,
    CareerCategory,
    EvaluatorKind,
    Language,
    LessonKind,
    OpportunitySource,
    OpportunityType,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
    Role,
    ScoreDimension,
    SkillCategory,
    TaskStatus,
    TaskSubmissionKind,
)
from app.core.security import create_token
from app.models.assessment import DevelopmentScore
from app.models.career_path import CareerPath, UserCareerPath
from app.models.learning_path import LearningPath, LearningPathItem, UserLearningPath
from app.models.opportunity import Application, Opportunity
from app.models.practice import PracticalTask, TaskAttempt
from app.models.profile import Profile
from app.models.program import Enrollment, Program, ProgramLesson, ProgramModule
from app.models.skill import Skill, SkillEvidence, UserSkill
from app.models.user import User
from app.services import ai_coach, learning, practice

CAREERS = "/api/v1/career-paths"
ME = "/api/v1/career-paths/me"
PROJECTS = "/api/v1/portfolio/me/projects"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


def _headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_token(user_id, Role.USER, "access", {"roles": [Role.USER.value]})
    return {"Authorization": f"Bearer {token}"}


async def _woman(
    session, user_id: uuid.UUID, *, born: date | None = date(1990, 5, 1), skills=()
) -> Profile:
    session.add(User(id=user_id, phone=_phone(), language=Language.UZ))
    await session.flush()
    profile = Profile(
        user_id=user_id, full_name="Dilnoza Karimova", birth_date=born, skills=list(skills)
    )
    session.add(profile)
    await session.flush()
    return profile


async def _skill(session, slug: str, label: str, *aliases: str, dimensions: tuple = ()) -> Skill:
    skill = Skill(
        slug=slug,
        name_i18n={"uz": label, "ru": label, "en": label},
        category=SkillCategory.PROFESSIONAL,
        dimensions=[dimension.value for dimension in dimensions],
        aliases=[slug, label.lower(), *aliases],
        is_curated=True,
    )
    session.add(skill)
    await session.flush()
    return skill


async def _course(session, *, taught: list[str], published: bool = True) -> Program:
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Buxgalteriya asoslari", "en": "Bookkeeping basics"},
        goal_i18n={},
        description_i18n={},
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        level=ProficiencyLevel.BEGINNER,
        learning_outcomes=[],
        skills_taught=taught,
        has_certificate=True,
        is_published=published,
        published_at=datetime.now(UTC),
    )
    session.add(program)
    await session.flush()
    module = ProgramModule(program_id=program.id, order_index=0, title_i18n={"uz": "Modul"})
    session.add(module)
    await session.flush()
    session.add(
        ProgramLesson(
            module_id=module.id,
            order_index=0,
            slug="1-dars",
            title_i18n={"uz": "Dars"},
            kind=LessonKind.READING,
            blocks=[],
        )
    )
    await session.flush()
    return program


async def _finish(session, user_id: uuid.UUID, program: Program) -> Enrollment:
    """Complete a course the way the platform does — through `services.learning`."""
    enrollment = await learning.enroll(session, user_id=user_id, program_id=program.id)
    lesson_id = await session.scalar(
        select(ProgramLesson.id)
        .join(ProgramModule, ProgramModule.id == ProgramLesson.module_id)
        .where(ProgramModule.program_id == program.id)
    )
    await learning.mark_lesson(session, enrollment=enrollment, lesson_id=lesson_id, program=program)
    return enrollment


async def _task(
    session, *, practises: list[str], published: bool = True, slug: str | None = None
) -> PracticalTask:
    task = PracticalTask(
        slug=slug or f"task-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Oylik hisobot", "en": "Monthly report"},
        summary_i18n={},
        instructions_i18n={"uz": "Yozing"},
        outcome_i18n={},
        criteria=[{"key": "a", "text_i18n": {"uz": "A"}}],
        kind=TaskSubmissionKind.TEXT,
        min_chars=10,
        level=ProficiencyLevel.ELEMENTARY,
        skills_practised=practises,
        is_published=published,
        published_at=datetime.now(UTC),
    )
    session.add(task)
    await session.flush()
    return task


async def _attempt(session, user_id: uuid.UUID, task: PracticalTask, *, passed: bool) -> None:
    attempt = TaskAttempt(
        task_id=task.id,
        user_id=user_id,
        attempt_no=1,
        status=TaskStatus.SUBMITTED,
        submission={"text": "Oylik hisobotni tayyorladim."},
        submitted_at=datetime.now(UTC),
    )
    session.add(attempt)
    await session.flush()
    await practice.record_evaluation(
        session,
        attempt=attempt,
        task=task,
        passed=passed,
        score=100 if passed else 40,
        feedback="Balansni qayta tekshiring.",
        criteria_met=[],
        evaluator_kind=EvaluatorKind.TRAINER,
        evaluator_id=None,
    )


async def _listing(
    session,
    *,
    skills: list[str],
    kind: OpportunityType = OpportunityType.VACANCY,
    active: bool = True,
    days: int | None = 20,
    title: str = "Buxgalter (kichik biznes)",
) -> Opportunity:
    opportunity = Opportunity(
        source=OpportunitySource.EDU_JOB,
        external_id=uuid.uuid4().hex,
        type=kind,
        title_i18n={"uz": title, "en": title},
        description_i18n={},
        organisation="Alfa Savdo",
        required_skills=skills,
        eligibility={},
        reward={},
        deadline=datetime.now(UTC) + timedelta(days=days) if days is not None else None,
        is_active=active,
    )
    session.add(opportunity)
    await session.flush()
    return opportunity


async def _learning_path(session, programs: list[Program], slug: str = "ishga-yol") -> LearningPath:
    path = LearningPath(
        slug=slug,
        title_i18n={"uz": "Birinchi ishga yoʻl", "en": "First job"},
        description_i18n={},
        dimension=ScoreDimension.EMPLOYMENT,
        level=ProficiencyLevel.BEGINNER,
        is_published=True,
    )
    session.add(path)
    await session.flush()
    for index, program in enumerate(programs):
        session.add(LearningPathItem(path_id=path.id, program_id=program.id, order_index=index))
    await session.flush()
    await session.refresh(path, ["items"])
    return path


async def _career(
    session,
    *,
    slug: str = "buxgalter",
    skills: list[str] = ("accounting", "excel"),
    category: CareerCategory = CareerCategory.EMPLOYMENT,
    types: list[OpportunityType] = (OpportunityType.VACANCY, OpportunityType.INTERNSHIP),
    learning_path: LearningPath | None = None,
    published: bool = True,
    order: int = 0,
    title: str = "Buxgalter",
) -> CareerPath:
    career = CareerPath(
        slug=slug,
        title_i18n={"uz": title, "ru": title, "en": title},
        summary_i18n={"uz": "Hisob yuritadi."},
        description_i18n={"uz": "Bu yoʻl tayyorlanishga yordam beradi."},
        category=category,
        level=ProficiencyLevel.ELEMENTARY,
        skill_slugs=list(skills),
        learning_path_id=learning_path.id if learning_path else None,
        opportunity_types=[kind.value for kind in types],
        order_index=order,
        is_published=published,
        published_at=datetime.now(UTC),
    )
    session.add(career)
    await session.flush()
    return career


async def _bookkeeping(session):
    """The skills, one course teaching both, one task and one vacancy."""
    await _skill(
        session,
        "accounting",
        "Buxgalteriya",
        "бухгалтерия",
        dimensions=(ScoreDimension.EMPLOYMENT,),
    )
    await _skill(session, "excel", "Excel", dimensions=(ScoreDimension.EDUCATION_SKILLS,))
    course = await _course(session, taught=["buxgalteriya", "excel"])
    task = await _task(session, practises=["buxgalteriya"])
    listing = await _listing(session, skills=["buxgalteriya", "excel"])
    return course, task, listing


def _stage(detail: dict, name: str) -> dict:
    return next(stage for stage in detail["stages"] if stage["stage"] == name)


# --- the catalogue ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_catalogue_is_open_and_lists_published_directions_in_order(client, session):
    await _bookkeeping(session)
    await _career(session, slug="ikkinchi", order=1, title="Ikkinchi")
    await _career(session, slug="birinchi", order=0, title="Birinchi")
    await _career(session, slug="yashirin", published=False)

    response = await client.get(CAREERS)

    assert response.status_code == 200
    body = response.json()
    assert [card["slug"] for card in body] == ["birinchi", "ikkinchi"]
    card = body[0]
    # A visitor gets the direction, never a personal reading of it.
    assert card["fit"] is None
    assert card["suggested"] is False
    assert card["counts"] == {"programs": 1, "tasks": 1, "opportunities": 1}
    assert [skill["slug"] for skill in card["skills"]] == ["accounting", "excel"]
    # Its dimensions are its skills' own, not a stored copy.
    assert set(card["dimensions"]) == {"employment", "education_skills"}


@pytest.mark.asyncio
async def test_the_catalogue_filters_by_category(client, session):
    await _bookkeeping(session)
    await _career(session, slug="ish")
    await _career(session, slug="biznes", category=CareerCategory.OWN_BUSINESS)

    response = await client.get(CAREERS, params={"category": "own_business"})

    assert [card["slug"] for card in response.json()] == ["biznes"]


@pytest.mark.asyncio
async def test_an_empty_catalogue_is_an_empty_list_not_an_error(client, session):
    response = await client.get(CAREERS)
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_an_unknown_or_unpublished_direction_is_404(client, session):
    await _career(session, slug="yashirin", published=False)

    assert (await client.get(f"{CAREERS}/yoq")).status_code == 404
    assert (await client.get(f"{CAREERS}/yashirin")).status_code == 404


# --- what a direction names -------------------------------------------------


@pytest.mark.asyncio
async def test_a_direction_names_only_real_live_records_of_the_right_kind(client, session):
    course, task, listing = await _bookkeeping(session)
    unrelated_course = await _course(session, taught=["tikuvchilik"])
    hidden_course = await _course(session, taught=["buxgalteriya"], published=False)
    hidden_task = await _task(session, practises=["excel"], published=False)
    closed = await _listing(session, skills=["excel"], days=-1, title="Muddati oʻtgan")
    inactive = await _listing(session, skills=["excel"], active=False, title="Yopilgan")
    grant = await _listing(session, skills=["excel"], kind=OpportunityType.GRANT, title="Grant")
    await _career(session)

    detail = (await client.get(f"{CAREERS}/buxgalter")).json()

    assert [p["id"] for p in detail["programs"]] == [str(course.id)]
    assert [t["id"] for t in detail["tasks"]] == [str(task.id)]
    assert [o["id"] for o in detail["opportunities"]] == [str(listing.id)]
    named = {p["id"] for p in detail["programs"]} | {t["id"] for t in detail["tasks"]}
    named |= {o["id"] for o in detail["opportunities"]}
    for record in (unrelated_course, hidden_course, hidden_task, closed, inactive, grant):
        assert str(record.id) not in named

    # Every name on the page resolves to a row that exists.
    for program in detail["programs"]:
        assert await session.get(Program, uuid.UUID(program["id"])) is not None
    for item in detail["opportunities"]:
        assert await session.get(Opportunity, uuid.UUID(item["id"])) is not None


@pytest.mark.asyncio
async def test_skills_match_through_the_taxonomy_not_the_spelling(client, session):
    await _skill(session, "accounting", "Buxgalteriya", "бухгалтерия", "buxgalteriya")
    russian = await _course(session, taught=["Бухгалтерия"])
    await _career(session, skills=["accounting"])

    detail = (await client.get(f"{CAREERS}/buxgalter")).json()

    assert [p["id"] for p in detail["programs"]] == [str(russian.id)]
    [skill] = detail["skill_details"]
    assert skill["skill"]["slug"] == "accounting"
    assert skill["programs"] == 1


@pytest.mark.asyncio
async def test_a_skill_nothing_teaches_is_shown_as_such_not_hidden(client, session):
    await _bookkeeping(session)
    await _skill(session, "overlock", "Overlok")
    await _career(session, skills=["accounting", "overlock"])

    detail = (await client.get(f"{CAREERS}/buxgalter")).json()

    overlock = next(s for s in detail["skill_details"] if s["skill"]["slug"] == "overlock")
    assert (overlock["programs"], overlock["tasks"], overlock["opportunities"]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_a_stage_the_catalogue_cannot_serve_is_unavailable_with_a_reason(
    client, session, user_id
):
    await _woman(session, user_id)
    await _skill(session, "sewing", "Tikuvchilik", "tikuvchilik")
    await _course(session, taught=["tikuvchilik"])
    await _career(session, slug="tikuvchi", skills=["sewing"])

    detail = (await client.get(f"{CAREERS}/tikuvchi", headers=_headers(user_id))).json()

    assert _stage(detail, "practice") == {
        "stage": "practice",
        "status": "unavailable",
        "reason": "no_tasks",
        "done": 0,
        "total": 0,
    }
    assert _stage(detail, "explore")["reason"] == "no_listings"
    # An empty stage is skipped, never where she is told to go.
    assert detail["journey"]["current"] == "learn"


# --- a visitor, and privacy -------------------------------------------------


@pytest.mark.asyncio
async def test_a_visitor_sees_the_direction_but_no_personal_state(client, session):
    await _bookkeeping(session)
    await _career(session)

    detail = (await client.get(f"{CAREERS}/buxgalter")).json()

    assert detail["journey"] is None
    assert detail["fit"] is None
    assert all(skill["status"] is None for skill in detail["skill_details"])
    assert all(stage["status"] is None for stage in detail["stages"])
    # "0% match" would read as a verdict on somebody who has not signed in.
    assert all(item["match"] is None for item in detail["opportunities"])


@pytest.mark.asyncio
async def test_personal_routes_need_an_account(client, session):
    await _career(session)

    assert (await client.get(ME)).status_code == 401
    assert (await client.put(ME, json={"slug": "buxgalter"})).status_code == 401
    assert (await client.delete(ME)).status_code == 401


@pytest.mark.asyncio
async def test_each_woman_sees_only_her_own_choice(client, session, user_id):
    other = uuid.uuid4()
    await _woman(session, user_id)
    await _woman(session, other)
    await _bookkeeping(session)
    await _career(session)

    assert (await client.put(ME, json={"slug": "buxgalter"}, headers=_headers(user_id))).is_success

    assert (await client.get(ME, headers=_headers(other))).json() is None
    theirs = (await client.get(CAREERS, headers=_headers(other))).json()
    assert theirs[0]["fit"]["chosen"] is False
    detail = (await client.get(f"{CAREERS}/buxgalter", headers=_headers(other))).json()
    assert detail["journey"]["chosen"] is False


@pytest.mark.asyncio
async def test_there_is_no_route_that_takes_a_user_id(client):
    paths = (await client.get("/api/v1/openapi.json")).json()["paths"]
    career_routes = [path for path in paths if path.startswith(CAREERS)]
    assert career_routes
    assert not any("user" in path for path in career_routes)


# --- choosing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_choosing_is_idempotent_and_one_direction_at_a_time(client, session, user_id):
    await _woman(session, user_id)
    await _bookkeeping(session)
    await _career(session, slug="birinchi")
    await _career(session, slug="ikkinchi", order=1)
    headers = _headers(user_id)

    first = await client.put(ME, json={"slug": "birinchi"}, headers=headers)
    again = await client.put(ME, json={"slug": "birinchi"}, headers=headers)
    assert first.status_code == again.status_code == 200
    assert first.json()["journey"]["chosen"] is True
    assert first.json()["journey"]["chosen_at"] == again.json()["journey"]["chosen_at"]

    await client.put(ME, json={"slug": "ikkinchi"}, headers=headers)

    rows = (
        await session.execute(select(UserCareerPath).where(UserCareerPath.user_id == user_id))
    ).scalars()
    assert len(list(rows)) == 1
    assert (await client.get(ME, headers=headers)).json()["slug"] == "ikkinchi"


@pytest.mark.asyncio
async def test_choosing_an_unknown_or_unpublished_direction_is_404(client, session, user_id):
    await _woman(session, user_id)
    await _career(session, slug="yashirin", published=False)
    headers = _headers(user_id)

    assert (await client.put(ME, json={"slug": "yoq"}, headers=headers)).status_code == 404
    assert (await client.put(ME, json={"slug": "yashirin"}, headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_choosing_starts_enrols_and_applies_to_nothing(client, session, user_id):
    await _woman(session, user_id)
    course, _, _ = await _bookkeeping(session)
    path = await _learning_path(session, [course])
    await _career(session, learning_path=path)

    await client.put(ME, json={"slug": "buxgalter"}, headers=_headers(user_id))

    for model in (Enrollment, UserLearningPath, Application, TaskAttempt):
        count = await session.scalar(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        )
        assert count == 0, model.__name__


@pytest.mark.asyncio
async def test_clearing_forgets_the_direction_and_nothing_else(client, session, user_id):
    await _woman(session, user_id)
    course, _, _ = await _bookkeeping(session)
    await _finish(session, user_id, course)
    await _career(session)
    headers = _headers(user_id)
    await client.put(ME, json={"slug": "buxgalter"}, headers=headers)

    assert (await client.delete(ME, headers=headers)).status_code == 204
    assert (await client.delete(ME, headers=headers)).status_code == 204

    assert (await client.get(ME, headers=headers)).json() is None
    enrollment = await session.scalar(select(Enrollment).where(Enrollment.user_id == user_id))
    assert enrollment is not None


@pytest.mark.asyncio
async def test_a_direction_that_is_unpublished_after_she_chose_it_is_no_longer_hers(
    client, session, user_id
):
    await _woman(session, user_id)
    career = await _career(session)
    headers = _headers(user_id)
    await client.put(ME, json={"slug": "buxgalter"}, headers=headers)

    career.is_published = False
    await session.flush()

    assert (await client.get(ME, headers=headers)).json() is None


# --- her fit ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_fit_counts_the_skills_she_holds_and_suggests_one_direction(client, session, user_id):
    await _woman(session, user_id, skills=["Excel"])
    await _bookkeeping(session)
    await _skill(session, "sewing", "Tikuvchilik")
    await _career(session, slug="tikuvchi", skills=["sewing"], order=0)
    await _career(session, slug="buxgalter", order=1)

    cards = (await client.get(CAREERS, headers=_headers(user_id))).json()

    by_slug = {card["slug"]: card for card in cards}
    assert by_slug["buxgalter"]["fit"] == {"have": 1, "total": 2, "chosen": False}
    assert by_slug["tikuvchi"]["fit"]["have"] == 0
    assert [card["slug"] for card in cards if card["suggested"]] == ["buxgalter"]


@pytest.mark.asyncio
async def test_nothing_is_suggested_when_she_holds_none_of_the_skills_or_has_chosen(
    client, session, user_id
):
    await _woman(session, user_id)
    await _bookkeeping(session)
    await _career(session)
    headers = _headers(user_id)

    assert not any(
        card["suggested"] for card in (await client.get(CAREERS, headers=headers)).json()
    )

    await _woman(session, other := uuid.uuid4(), skills=["Excel"])
    await client.put(ME, json={"slug": "buxgalter"}, headers=_headers(other))
    cards = (await client.get(CAREERS, headers=_headers(other))).json()
    assert not any(card["suggested"] for card in cards)
    assert cards[0]["fit"]["chosen"] is True


# --- the journey, read from her records -------------------------------------


@pytest.mark.asyncio
async def test_the_journey_moves_only_when_her_records_do(client, session, user_id):
    await _woman(session, user_id)
    course, task, listing = await _bookkeeping(session)
    await _career(session)
    headers = _headers(user_id)

    async def read() -> dict:
        return (await client.get(f"{CAREERS}/buxgalter", headers=headers)).json()

    detail = await read()
    assert [stage["status"] for stage in detail["stages"]] == ["todo", "todo", "todo", "todo"]
    assert detail["journey"]["current"] == "learn"
    step = detail["journey"]["next_step"]
    assert step["kind"] == "start_program"
    assert step["program"]["id"] == str(course.id)

    # A finished course makes both skills learned: "Learn" is done.
    await _finish(session, user_id, course)
    detail = await read()
    assert _stage(detail, "learn")["status"] == "done"
    assert {s["skill"]["slug"]: s["status"] for s in detail["skill_details"]} == {
        "accounting": "learned",
        "excel": "learned",
    }
    assert detail["journey"]["current"] == "practice"
    assert detail["journey"]["next_step"]["task"]["id"] == str(task.id)

    # A passed task: "Practice" is done, and the certificate and the task are
    # evidence she can already show.
    await _attempt(session, user_id, task, passed=True)
    detail = await read()
    assert _stage(detail, "practice") == {
        "stage": "practice",
        "status": "done",
        "reason": None,
        "done": 1,
        "total": 1,
    }
    assert detail["journey"]["current"] == "build"
    assert _stage(detail, "build")["status"] == "in_progress"
    assert detail["journey"]["next_step"]["kind"] == "add_project"
    assert len(detail["journey"]["evidence"]["passed_tasks"]) == 1

    # Her own write-up of work on the direction: "Build" is done.
    response = await client.post(
        PROJECTS,
        json={"title": "Doʻkon uchun hisob jadvali", "skills": ["accounting"]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    detail = await read()
    assert _stage(detail, "build")["status"] == "done"
    assert detail["journey"]["current"] == "explore"
    assert detail["journey"]["next_step"]["kind"] == "explore_opportunities"

    # She applied: every stage WomanUP can offer is done.
    session.add(
        Application(
            user_id=user_id,
            opportunity_id=listing.id,
            status=ApplicationStatus.SUBMITTED,
            payload={},
            status_history=[],
        )
    )
    await session.flush()
    detail = await read()
    assert _stage(detail, "explore")["status"] == "done"
    assert detail["journey"]["current"] is None
    assert detail["journey"]["next_step"] is None
    assert detail["journey"]["applications"] == 1


@pytest.mark.asyncio
async def test_a_draft_or_withdrawn_application_is_not_applying(client, session, user_id):
    await _woman(session, user_id)
    _, _, listing = await _bookkeeping(session)
    second = await _listing(session, skills=["excel"], title="Excel operatori")
    await _career(session)
    for status, target in (
        (ApplicationStatus.DRAFT, listing),
        (ApplicationStatus.WITHDRAWN, second),
    ):
        session.add(
            Application(
                user_id=user_id,
                opportunity_id=target.id,
                status=status,
                payload={},
                status_history=[],
            )
        )
        await session.flush()

    detail = (await client.get(f"{CAREERS}/buxgalter", headers=_headers(user_id))).json()

    assert _stage(detail, "explore")["status"] == "todo"
    assert detail["journey"]["applications"] == 0


@pytest.mark.asyncio
async def test_a_skill_she_only_listed_does_not_finish_learning(client, session, user_id):
    await _woman(session, user_id, skills=["Buxgalteriya", "Excel"])
    await _bookkeeping(session)
    await _career(session)

    detail = (await client.get(f"{CAREERS}/buxgalter", headers=_headers(user_id))).json()

    assert {s["status"] for s in detail["skill_details"]} == {"self_reported"}
    assert detail["fit"]["have"] == 2
    assert _stage(detail, "learn")["status"] == "todo"
    assert _stage(detail, "learn")["done"] == 0


@pytest.mark.asyncio
async def test_work_she_was_told_to_fix_is_the_next_practice_step(client, session, user_id):
    await _woman(session, user_id)
    course, task, _ = await _bookkeeping(session)
    await _finish(session, user_id, course)
    await _attempt(session, user_id, task, passed=False)
    await _career(session)

    detail = (await client.get(f"{CAREERS}/buxgalter", headers=_headers(user_id))).json()

    assert _stage(detail, "practice")["status"] == "in_progress"
    step = detail["journey"]["next_step"]
    assert step["kind"] == "improve_task"
    assert step["reason"] == "needs_improvement"


@pytest.mark.asyncio
async def test_the_learning_path_leads_when_she_has_started_nothing(client, session, user_id):
    await _woman(session, user_id)
    course, _, _ = await _bookkeeping(session)
    path = await _learning_path(session, [course])
    await _career(session, learning_path=path)

    detail = (await client.get(f"{CAREERS}/buxgalter", headers=_headers(user_id))).json()

    assert detail["learning_path"]["slug"] == "ishga-yol"
    step = detail["journey"]["next_step"]
    assert step["kind"] == "start_path"
    assert step["path"]["slug"] == "ishga-yol"


@pytest.mark.asyncio
async def test_reading_a_direction_changes_no_evidence(client, session, user_id):
    await _woman(session, user_id, skills=["Excel"])
    course, _, _ = await _bookkeeping(session)
    await _finish(session, user_id, course)
    await _career(session)

    async def evidence() -> list:
        return sorted(
            (row.kind.value, row.source_type, row.revoked_at is None)
            for row in (
                await session.execute(
                    select(SkillEvidence)
                    .join(UserSkill, UserSkill.id == SkillEvidence.user_skill_id)
                    .where(UserSkill.user_id == user_id)
                )
            ).scalars()
        )

    before = await evidence()
    await client.get(f"{CAREERS}/buxgalter", headers=_headers(user_id))
    await client.get(CAREERS, headers=_headers(user_id))
    assert await evidence() == before


# --- age and the score ------------------------------------------------------


@pytest.mark.asyncio
async def test_listings_are_withheld_from_a_girl_under_18(client, session, user_id):
    today = date.today()
    await _woman(session, user_id, born=today.replace(year=today.year - 15))
    await _bookkeeping(session)
    await _career(session)
    headers = _headers(user_id)

    detail = (await client.get(f"{CAREERS}/buxgalter", headers=headers)).json()

    assert detail["opportunities"] == []
    assert detail["counts"]["opportunities"] == 0
    assert all(skill["opportunities"] == 0 for skill in detail["skill_details"])
    assert _stage(detail, "explore")["status"] == "unavailable"
    assert _stage(detail, "explore")["reason"] == "adults_only"
    # Learning and practice are still hers.
    assert detail["programs"]
    assert detail["tasks"]
    card = (await client.get(CAREERS, headers=headers)).json()[0]
    assert card["counts"]["opportunities"] == 0


@pytest.mark.asyncio
async def test_an_adult_sees_the_listings(client, session, user_id):
    await _woman(session, user_id)
    _, _, listing = await _bookkeeping(session)
    await _career(session)

    detail = (await client.get(f"{CAREERS}/buxgalter", headers=_headers(user_id))).json()

    assert [item["id"] for item in detail["opportunities"]] == [str(listing.id)]
    assert detail["opportunities"][0]["match"] == 0.0


@pytest.mark.asyncio
async def test_a_weak_dimension_is_explained_and_the_score_is_untouched(client, session, user_id):
    await _woman(session, user_id)
    await _bookkeeping(session)
    await _career(session)
    session.add_all(
        [
            DevelopmentScore(
                user_id=user_id, dimension=ScoreDimension.EMPLOYMENT, baseline=20, current=20
            ),
            DevelopmentScore(
                user_id=user_id, dimension=ScoreDimension.EDUCATION_SKILLS, baseline=95, current=95
            ),
        ]
    )
    await session.flush()

    detail = (await client.get(f"{CAREERS}/buxgalter", headers=_headers(user_id))).json()

    # A strong area is not "worth developing"; the weak one is, with her own score.
    assert detail["journey"]["dimension_notes"] == [
        {"dimension": "employment", "score": 20, "band": "focus"}
    ]
    scores = (
        await session.execute(select(DevelopmentScore).where(DevelopmentScore.user_id == user_id))
    ).scalars()
    assert sorted(score.current for score in scores) == [20, 95]


@pytest.mark.asyncio
async def test_without_a_score_there_is_no_dimension_note(client, session, user_id):
    await _woman(session, user_id)
    await _bookkeeping(session)
    await _career(session)

    detail = (await client.get(f"{CAREERS}/buxgalter", headers=_headers(user_id))).json()

    assert detail["journey"]["dimension_notes"] == []


# --- the Coach --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_coach_knows_only_real_directions_and_her_choice(client, session, user_id):
    await _woman(session, user_id, skills=["Excel"])
    await _bookkeeping(session)
    await _career(session, title="Buxgalter")
    await _career(session, slug="yashirin", title="Yashirin kasb", published=False)
    await client.put(ME, json={"slug": "buxgalter"}, headers=_headers(user_id))

    context = await ai_coach.build(session, user_id, language="uz")

    prompt = context.as_prompt()
    assert "Buxgalter (she holds 1 of its 2 skills)" in prompt
    assert "her chosen career direction: Buxgalter" in prompt
    assert "still missing: Buxgalteriya" in prompt
    assert "Yashirin kasb" not in prompt
    kinds = {reference.kind for reference in context.offer.values()}
    assert "career_path" in kinds
    assert context.read.career is not None
    assert context.read.career.slug == "buxgalter"
    assert "coach.q.career" in {suggestion.key for suggestion in context.read.suggestions}


@pytest.mark.asyncio
async def test_the_coach_says_she_has_not_chosen_rather_than_guessing(client, session, user_id):
    await _woman(session, user_id)
    await _bookkeeping(session)
    await _career(session)

    context = await ai_coach.build(session, user_id, language="en")

    assert context.read.career is None
    assert "her chosen career direction: none yet" in context.as_prompt()


@pytest.mark.asyncio
async def test_the_coach_is_told_when_listings_are_withheld(client, session, user_id):
    today = date.today()
    await _woman(session, user_id, born=today.replace(year=today.year - 15))
    await _bookkeeping(session)
    await _career(session)
    await client.put(ME, json={"slug": "buxgalter"}, headers=_headers(user_id))

    context = await ai_coach.build(session, user_id, language="en")

    assert context.read.career.listings_withheld is True
    assert "she is under 18" in context.as_prompt()
    assert not any(
        reference.kind == "opportunity" and "on her chosen direction" in line
        for reference, line in zip(context.offer.values(), context.offer_lines, strict=True)
    )
