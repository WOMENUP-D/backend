"""The portfolio: a presentation layer that must never invent or leak.

What is pinned here, in order of how badly it would hurt to get wrong:

* A private record is never readable by anyone else, and a public page is never
  a way to reach a surname, a birth date, a submission, an evaluator's words,
  or a project she kept private.
* A minor's portfolio — or one belonging to a woman whose age is unknown — is
  never public.
* Achievements come only from records that exist, and the same event is never
  counted twice.
* Showing evidence never changes it: learned, assessed and verified stay
  distinct, and a project she wrote herself verifies nothing.
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.core.constants import (
    AchievementType,
    ConsentScope,
    EnrollmentStatus,
    EvaluatorKind,
    EvidenceKind,
    Language,
    LessonKind,
    OpportunitySource,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
    Role,
    SkillCategory,
    SkillStatus,
    TaskStatus,
    TaskSubmissionKind,
    UserStatus,
)
from app.core.security import create_token
from app.models.consent import ConsentLog
from app.models.learning_path import LearningPath, LearningPathItem
from app.models.opportunity import OutcomeRecord
from app.models.practice import PracticalTask, TaskAttempt
from app.models.profile import Profile
from app.models.program import Certificate, Enrollment, Program, ProgramLesson, ProgramModule
from app.models.skill import Skill, SkillEvidence, UserSkill
from app.models.user import User
from app.services import learning, learning_path, portfolio, practice, skills

ME = "/api/v1/portfolio/me"
PROJECTS = "/api/v1/portfolio/me/projects"
PUBLIC = "/api/v1/portfolio/public"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


def _headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_token(user_id, Role.USER, "access", {"roles": [Role.USER.value]})
    return {"Authorization": f"Bearer {token}"}


async def _woman(
    session, user_id: uuid.UUID, *, born: date | None = date(1990, 5, 1), name="Dilnoza Karimova"
) -> Profile:
    session.add(User(id=user_id, phone=_phone(), language=Language.UZ))
    await session.flush()
    profile = Profile(
        user_id=user_id,
        full_name=name,
        birth_date=born,
        profession="Buxgalter",
        bio="Kichik biznes uchun hisob yurityapman.",
        district="Chilonzor",
        skills=[],
    )
    session.add(profile)
    await session.flush()
    return profile


async def _skill(session, slug: str, label: str, *aliases: str) -> Skill:
    skill = Skill(
        slug=slug,
        name_i18n={"uz": label, "ru": label, "en": label},
        category=SkillCategory.DIGITAL,
        dimensions=[],
        aliases=[slug, label.lower(), *aliases],
        is_curated=True,
    )
    session.add(skill)
    await session.flush()
    return skill


async def _course(session, *, taught: list[str], certificate: bool = True) -> Program:
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
        has_certificate=certificate,
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(program)
    await session.flush()
    module = ProgramModule(program_id=program.id, order_index=0, title_i18n={"uz": "M"})
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


async def _task(session, *, slug: str, practises: list[str]) -> PracticalTask:
    task = PracticalTask(
        slug=slug,
        title_i18n={"uz": "Oylik byudjet", "en": "Monthly budget"},
        summary_i18n={},
        instructions_i18n={"uz": "Yozing"},
        outcome_i18n={},
        criteria=[{"key": "a", "text_i18n": {"uz": "A"}}],
        kind=TaskSubmissionKind.TEXT,
        min_chars=10,
        level=ProficiencyLevel.ELEMENTARY,
        skills_practised=practises,
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(task)
    await session.flush()
    return task


async def _attempt(
    session, user_id: uuid.UUID, task: PracticalTask, *, passed: bool
) -> TaskAttempt:
    attempt = TaskAttempt(
        task_id=task.id,
        user_id=user_id,
        attempt_no=1,
        status=TaskStatus.SUBMITTED,
        submission={"text": "Oilamizning daromadi 11 700 000 soʻm."},
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
        feedback="Evaluatorning maxfiy izohi.",
        criteria_met=[],
        evaluator_kind=EvaluatorKind.TRAINER,
        evaluator_id=None,
    )
    return attempt


async def _publish(client, user_id: uuid.UUID) -> str:
    response = await client.patch(ME, json={"is_public": True}, headers=_headers(user_id))
    assert response.status_code == 200, response.text
    return response.json()["slug"]


# --- ownership and privacy --------------------------------------------------


@pytest.mark.asyncio
async def test_the_portfolio_needs_an_account(client):
    assert (await client.get(ME)).status_code == 401
    assert (await client.post(PROJECTS, json={"title": "Loyiha"})).status_code == 401


@pytest.mark.asyncio
async def test_each_woman_sees_only_her_own_record(client, session, user_id):
    await _woman(session, user_id)
    other = uuid.uuid4()
    await _woman(session, other, name="Malika")
    await client.post(PROJECTS, json={"title": "Mening loyiham"}, headers=_headers(user_id))
    await client.post(PROJECTS, json={"title": "Uning loyihasi"}, headers=_headers(other))

    mine = (await client.get(ME, headers=_headers(user_id))).json()

    assert [p["title"] for p in mine["projects"]] == ["Mening loyiham"]
    assert mine["person"]["first_name"] == "Dilnoza"


@pytest.mark.asyncio
async def test_another_womans_project_id_is_a_plain_404(client, session, user_id):
    """Looked up by (id, caller) together, so her id says nothing."""
    await _woman(session, user_id)
    other = uuid.uuid4()
    await _woman(session, other, name="Malika")
    theirs = (
        await client.post(PROJECTS, json={"title": "Uning loyihasi"}, headers=_headers(other))
    ).json()

    edit = await client.patch(
        f"{PROJECTS}/{theirs['id']}", json={"title": "Buzildi"}, headers=_headers(user_id)
    )
    delete = await client.delete(f"{PROJECTS}/{theirs['id']}", headers=_headers(user_id))
    missing = await client.delete(f"{PROJECTS}/{uuid.uuid4()}", headers=_headers(user_id))

    assert edit.status_code == 404
    assert delete.status_code == 404
    assert missing.status_code == 404
    assert edit.json() == missing.json() | {"detail": "Project not found"}
    still = (await client.get(ME, headers=_headers(other))).json()
    assert [p["title"] for p in still["projects"]] == ["Uning loyihasi"]


@pytest.mark.asyncio
async def test_a_private_portfolio_is_not_public_and_every_refusal_looks_the_same(
    client, session, user_id
):
    profile = await _woman(session, user_id)
    profile.portfolio_slug = "private-slug-xyz"
    await session.flush()

    private = await client.get(f"{PUBLIC}/private-slug-xyz")
    unknown = await client.get(f"{PUBLIC}/no-such-slug-at-all")

    assert private.status_code == 404
    assert unknown.status_code == 404
    # No oracle: a real-but-private slug and a made-up one answer identically.
    assert private.json() == unknown.json()


@pytest.mark.asyncio
async def test_the_public_page_carries_only_what_she_chose_and_nothing_personal(
    client, session, user_id
):
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")
    program = await _course(session, taught=["Excel"])
    await _finish(session, user_id, program)
    task = await _task(session, slug="byudjet", practises=["Excel"])
    await _attempt(session, user_id, task, passed=True)
    await client.post(
        PROJECTS,
        json={"title": "Ochiq loyiha", "is_public": True, "completed_on": "2026-03-01"},
        headers=_headers(user_id),
    )
    await client.post(
        PROJECTS,
        json={"title": "Yashirin loyiha", "is_public": False, "completed_on": "2026-04-01"},
        headers=_headers(user_id),
    )
    slug = await _publish(client, user_id)

    body = (await client.get(f"{PUBLIC}/{slug}")).json()
    text = str(body)

    assert body["first_name"] == "Dilnoza"
    assert [p["title"] for p in body["projects"]] == ["Ochiq loyiha"]
    # A private project leaks through nothing — not its card, not an
    # achievement, not the evidence behind a skill.
    assert "Yashirin loyiha" not in text
    # Nothing personal, nothing internal.
    for private in (
        "Karimova",
        "1990",
        "Chilonzor",
        "Evaluatorning maxfiy izohi",
        "11 700 000",
        str(user_id),
    ):
        assert private not in text, private
    # Passed work shows its title and who assessed it, never what she wrote.
    [work] = body["practice"]
    assert work["status"] == "passed"
    assert "submission" not in work
    assert body["certificates"][0]["verification_code"]


@pytest.mark.asyncio
async def test_hidden_sections_are_absent_not_empty(client, session, user_id):
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")
    await _finish(session, user_id, await _course(session, taught=["Excel"]))
    slug = await _publish(client, user_id)
    await client.patch(
        ME,
        json={"sections": {"certificates": False, "practice": False}},
        headers=_headers(user_id),
    )

    body = (await client.get(f"{PUBLIC}/{slug}")).json()

    assert body["certificates"] is None
    assert body["practice"] is None
    assert body["skills"] is not None
    assert body["achievements"] is not None


@pytest.mark.asyncio
async def test_an_unknown_section_is_refused(client, session, user_id):
    await _woman(session, user_id)
    response = await client.patch(
        ME, json={"sections": {"salary": True}}, headers=_headers(user_id)
    )
    assert response.status_code == 422
    assert response.json()["detail"]["fields"] == ["salary"]


# --- who may publish -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_minor_cannot_publish(client, session, user_id):
    await _woman(session, user_id, born=date(date.today().year - 15, 1, 1))

    response = await client.patch(ME, json={"is_public": True}, headers=_headers(user_id))

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "minor"
    settings = (await client.get(ME, headers=_headers(user_id))).json()["settings"]
    assert settings["is_public"] is False
    assert settings["can_publish"] is False
    assert settings["publish_blocked"] == "minor"


@pytest.mark.asyncio
async def test_an_unknown_age_is_read_the_protective_way(client, session, user_id):
    await _woman(session, user_id, born=None)

    response = await client.patch(ME, json={"is_public": True}, headers=_headers(user_id))

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_page_published_by_an_adult_closes_if_her_age_is_corrected(
    client, session, user_id
):
    """Fails closed: eligibility is re-checked on every read, not just on publish."""
    profile = await _woman(session, user_id)
    slug = await _publish(client, user_id)
    assert (await client.get(f"{PUBLIC}/{slug}")).status_code == 200

    profile.birth_date = date(date.today().year - 16, 1, 1)
    await session.flush()

    assert (await client.get(f"{PUBLIC}/{slug}")).status_code == 404


@pytest.mark.asyncio
async def test_every_change_of_visibility_is_an_appended_consent(client, session, user_id):
    await _woman(session, user_id)
    slug = await _publish(client, user_id)
    await client.patch(ME, json={"is_public": False}, headers=_headers(user_id))

    rows = list(
        (
            await session.execute(
                select(ConsentLog)
                .where(
                    ConsentLog.user_id == user_id,
                    ConsentLog.scope == ConsentScope.PUBLIC_PORTFOLIO,
                )
                .order_by(ConsentLog.created_at)
            )
        ).scalars()
    )
    assert [row.accepted for row in rows] == [True, False]
    assert (await client.get(f"{PUBLIC}/{slug}")).status_code == 404


@pytest.mark.asyncio
async def test_a_withdrawn_consent_closes_the_page_even_if_the_flag_says_public(
    client, session, user_id
):
    """Two switches must agree. If they ever disagree the page is private."""
    await _woman(session, user_id)
    slug = await _publish(client, user_id)
    session.add(
        ConsentLog(
            user_id=user_id,
            scope=ConsentScope.PUBLIC_PORTFOLIO,
            accepted=False,
            policy_version="1.0",
            accepted_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()

    assert (await client.get(f"{PUBLIC}/{slug}")).status_code == 404


@pytest.mark.asyncio
async def test_a_suspended_account_has_no_public_page(client, session, user_id):
    await _woman(session, user_id)
    slug = await _publish(client, user_id)
    account = await session.get(User, user_id)
    account.status = UserStatus.SUSPENDED
    await session.flush()

    assert (await client.get(f"{PUBLIC}/{slug}")).status_code == 404


@pytest.mark.asyncio
async def test_the_public_slug_is_opaque_and_not_her_id(client, session, user_id):
    await _woman(session, user_id)
    slug = await _publish(client, user_id)

    assert str(user_id) not in slug
    assert str(user_id).replace("-", "")[:8] not in slug.lower()
    assert len(slug) >= 16


# --- projects ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_project_is_created_updated_and_deleted(client, session, user_id):
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")

    created = await client.post(
        PROJECTS,
        json={
            "title": "Doʻkon hisobi",
            "summary": "Kichik doʻkon uchun Excel jadvali",
            "skills": ["Excel"],
            "project_url": "https://example.com/hisob",
            "completed_on": "2026-02-10",
        },
        headers=_headers(user_id),
    )
    assert created.status_code == 201
    project = created.json()
    assert [s["slug"] for s in project["skills"]] == ["excel"]
    assert project["is_public"] is False

    updated = await client.patch(
        f"{PROJECTS}/{project['id']}",
        json={"title": "Doʻkon hisobi 2.0", "is_public": True},
        headers=_headers(user_id),
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Doʻkon hisobi 2.0"
    assert updated.json()["is_public"] is True

    deleted = await client.delete(f"{PROJECTS}/{project['id']}", headers=_headers(user_id))
    assert deleted.status_code == 204
    assert (await client.get(ME, headers=_headers(user_id))).json()["projects"] == []


@pytest.mark.asyncio
async def test_a_retried_save_is_the_same_project(client, session, user_id):
    await _woman(session, user_id)
    ref = str(uuid.uuid4())
    payload = {"title": "Bir loyiha", "client_ref": ref}

    first = await client.post(PROJECTS, json=payload, headers=_headers(user_id))
    second = await client.post(PROJECTS, json=payload, headers=_headers(user_id))

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len((await client.get(ME, headers=_headers(user_id))).json()["projects"]) == 1


@pytest.mark.asyncio
async def test_skills_resolve_through_the_taxonomy_and_unknown_ones_are_refused(
    client, session, user_id
):
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel", "эксель")

    spelled = await client.post(
        PROJECTS, json={"title": "Jadval", "skills": ["эксель", "EXCEL"]}, headers=_headers(user_id)
    )
    unknown = await client.post(
        PROJECTS,
        json={"title": "Boshqa", "skills": ["Excel", "Quantum knitting"]},
        headers=_headers(user_id),
    )

    # Two spellings, one skill.
    assert [s["slug"] for s in spelled.json()["skills"]] == ["excel"]
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == {"reason": "unknown_skill", "fields": ["Quantum knitting"]}
    # Nothing was minted.
    assert (await session.scalar(select(Skill).where(Skill.slug.like("%quantum%")))) is None


@pytest.mark.asyncio
async def test_only_http_links_are_accepted(client, session, user_id):
    await _woman(session, user_id)
    for bad in ("javascript:alert(1)", "data:text/html,x", "ftp://x.y", "not a link"):
        response = await client.post(
            PROJECTS, json={"title": "Havola", "demo_url": bad}, headers=_headers(user_id)
        )
        assert response.status_code == 422, bad
        assert response.json()["detail"] == {"reason": "bad_url", "fields": ["demo_url"]}


# --- achievements -----------------------------------------------------------


async def _achievements(session, user_id) -> list:
    return await portfolio.achievements_for(session, user_id)


@pytest.mark.asyncio
async def test_a_finished_course_is_one_achievement_however_often_it_is_processed(session, user_id):
    await _woman(session, user_id)
    program = await _course(session, taught=["Excel"], certificate=True)
    enrollment = await _finish(session, user_id, program)
    # Replayed: the same completion recomputed twice more.
    await learning.recompute(session, enrollment=enrollment, program=program)
    await learning.recompute(session, enrollment=enrollment, program=program)

    found = await _achievements(session, user_id)
    kinds = [a.type for a in found]

    assert kinds.count(AchievementType.COURSE_COMPLETED) == 1
    assert kinds.count(AchievementType.CERTIFICATE_EARNED) == 1
    assert len({a.key for a in found}) == len(found)


@pytest.mark.asyncio
async def test_a_finished_path_is_an_achievement(session, user_id):
    await _woman(session, user_id)
    program = await _course(session, taught=["Excel"], certificate=False)
    path = LearningPath(
        slug="yol", title_i18n={"uz": "Yoʻl"}, description_i18n={}, is_published=True
    )
    session.add(path)
    await session.flush()
    session.add(LearningPathItem(path_id=path.id, program_id=program.id, order_index=0))
    await session.flush()
    await session.refresh(path, ["items"])
    await learning_path.start(session, user_id=user_id, path=path)

    await _finish(session, user_id, program)

    [done] = [
        a
        for a in await _achievements(session, user_id)
        if a.type == AchievementType.LEARNING_PATH_COMPLETED
    ]
    assert done.title_i18n["uz"] == "Yoʻl"


@pytest.mark.asyncio
async def test_a_passed_task_is_an_achievement_and_a_failed_one_is_not(session, user_id):
    await _woman(session, user_id)
    passed_task = await _task(session, slug="otdi", practises=[])
    failed_task = await _task(session, slug="otmadi", practises=[])
    await _attempt(session, user_id, passed_task, passed=True)
    await _attempt(session, user_id, failed_task, passed=False)

    found = [
        a
        for a in await _achievements(session, user_id)
        if a.type == AchievementType.PRACTICAL_TASK_PASSED
    ]

    assert len(found) == 1
    assert found[0].href == "/talim/amaliyot/otdi"
    assert found[0].detail == "trainer"


@pytest.mark.asyncio
async def test_a_verified_skill_is_an_achievement_dated_by_what_verified_it(session, user_id):
    await _woman(session, user_id)
    skill = await _skill(session, "excel", "Excel")
    await skills.record_course_completion(
        session, user_id=user_id, labels=["Excel"], enrollment_id=uuid.uuid4()
    )
    await skills.record_evidence(
        session,
        user_id=user_id,
        skill=skill,
        kind=EvidenceKind.MENTOR_ASSESSMENT,
        source_type="mentor_session",
        source_id=uuid.uuid4().hex,
    )

    [verified] = [
        a for a in await _achievements(session, user_id) if a.type == AchievementType.SKILL_VERIFIED
    ]

    assert verified.detail == "mentor_assessment"
    assert verified.title_i18n["uz"] == "Excel"


@pytest.mark.asyncio
async def test_a_course_never_makes_a_skill_verified(client, session, user_id):
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")
    await _finish(session, user_id, await _course(session, taught=["Excel"]))

    body = (await client.get(ME, headers=_headers(user_id))).json()

    assert [s["skill"]["slug"] for s in body["skills"]["learned"]] == ["excel"]
    assert body["skills"]["verified"] == []
    assert body["overview"]["verified"] == 0
    assert all(a["type"] != "skill_verified" for a in body["achievements"])


@pytest.mark.asyncio
async def test_a_project_is_an_achievement_only_when_finished_and_says_it_is_hers(
    client, session, user_id
):
    await _woman(session, user_id)
    await client.post(
        PROJECTS, json={"title": "Tugagan", "completed_on": "2026-01-15"}, headers=_headers(user_id)
    )
    await client.post(PROJECTS, json={"title": "Davom etmoqda"}, headers=_headers(user_id))

    found = [
        a
        for a in await _achievements(session, user_id)
        if a.type == AchievementType.PROJECT_COMPLETED
    ]

    assert [a.title_i18n["uz"] for a in found] == ["Tugagan"]
    assert found[0].self_declared is True


@pytest.mark.asyncio
async def test_only_confirmed_partner_outcomes_are_achievements(session, user_id):
    await _woman(session, user_id)
    now = datetime.now(UTC)
    session.add_all(
        [
            OutcomeRecord(
                user_id=user_id,
                source=OpportunitySource.INTERNAL,
                outcome_type="employment",
                verified=True,
                occurred_at=now,
                details={"employer": "should never be read"},
            ),
            OutcomeRecord(
                user_id=user_id,
                source=OpportunitySource.INTERNAL,
                outcome_type="first_sale",
                verified=False,
                occurred_at=now,
            ),
        ]
    )
    await session.flush()

    found = await _achievements(session, user_id)

    assert [a.type for a in found] == [AchievementType.WORK_EXPERIENCE]
    assert "should never be read" not in str(found)


@pytest.mark.asyncio
async def test_a_profile_skill_is_not_an_achievement(session, user_id):
    """Listing "Python" is her word. A word is not an achievement."""
    profile = await _woman(session, user_id)
    profile.skills = ["Python", "Excel"]
    await skills.sync_self_reported(session, user_id, profile.skills)

    assert await _achievements(session, user_id) == []


# --- certificates -----------------------------------------------------------


@pytest.mark.asyncio
async def test_only_her_real_certificates_appear(client, session, user_id):
    await _woman(session, user_id)
    other = uuid.uuid4()
    await _woman(session, other, name="Malika")
    program = await _course(session, taught=["Excel"])
    await _finish(session, user_id, program)
    await _finish(session, other, program)
    revoked = await _finish(session, user_id, await _course(session, taught=["Excel"]))
    cert = await session.scalar(select(Certificate).where(Certificate.enrollment_id == revoked.id))
    cert.revoked_at = datetime.now(UTC)
    await session.flush()

    body = (await client.get(ME, headers=_headers(user_id))).json()

    assert len(body["certificates"]) == 1
    assert body["overview"]["certificates"] == 1
    theirs = await session.scalar(select(Certificate).where(Certificate.user_id == other))
    assert theirs.serial_number not in str(body)


# --- skills -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_learned_assessed_and_verified_stay_in_separate_groups(client, session, user_id):
    await _woman(session, user_id)
    taught = await _skill(session, "excel", "Excel")
    scored = await _skill(session, "budget", "Byudjet")
    vouched = await _skill(session, "sales", "Sotuv")
    for skill, kind in (
        (taught, EvidenceKind.COURSE_COMPLETION),
        (scored, EvidenceKind.FORMAL_ASSESSMENT),
        (vouched, EvidenceKind.EMPLOYER_ASSESSMENT),
    ):
        await skills.record_evidence(
            session,
            user_id=user_id,
            skill=skill,
            kind=kind,
            source_type="test",
            source_id=uuid.uuid4().hex,
        )

    groups = (await client.get(ME, headers=_headers(user_id))).json()["skills"]

    assert [e["skill"]["slug"] for e in groups["learned"]] == ["excel"]
    assert [e["skill"]["slug"] for e in groups["assessed"]] == ["budget"]
    assert [e["skill"]["slug"] for e in groups["verified"]] == ["sales"]


@pytest.mark.asyncio
async def test_reading_the_portfolio_changes_no_evidence(client, session, user_id):
    await _woman(session, user_id)
    await _skill(session, "excel", "Excel")
    await _finish(session, user_id, await _course(session, taught=["Excel"]))

    async def snapshot():
        rows = await session.execute(
            select(UserSkill.status, UserSkill.level, SkillEvidence.kind, SkillEvidence.revoked_at)
            .join(SkillEvidence, SkillEvidence.user_skill_id == UserSkill.id)
            .where(UserSkill.user_id == user_id)
        )
        return sorted(map(str, rows.all()))

    before = await snapshot()
    for _ in range(3):
        await client.get(ME, headers=_headers(user_id))
    assert await snapshot() == before


@pytest.mark.asyncio
async def test_a_project_never_raises_a_skill_above_her_own_word(client, session, user_id):
    await _woman(session, user_id)
    await _skill(session, "react", "React")

    await client.post(
        PROJECTS,
        json={"title": "React sayt", "skills": ["React"], "completed_on": "2026-01-01"},
        headers=_headers(user_id),
    )

    record = await session.scalar(select(UserSkill).where(UserSkill.user_id == user_id))
    assert record.status == SkillStatus.SELF_REPORTED
    evidence = list(
        (
            await session.execute(
                select(SkillEvidence).where(SkillEvidence.user_skill_id == record.id)
            )
        ).scalars()
    )
    assert [(e.kind, e.source_type) for e in evidence] == [
        (EvidenceKind.SELF_REPORTED, "portfolio_project")
    ]
    # And a self-reported skill is not a portfolio skill.
    groups = (await client.get(ME, headers=_headers(user_id))).json()["skills"]
    assert groups == {"verified": [], "assessed": [], "learned": []}


@pytest.mark.asyncio
async def test_a_project_does_not_weaken_a_skill_she_already_proved(client, session, user_id):
    await _woman(session, user_id)
    skill = await _skill(session, "excel", "Excel")
    await skills.record_evidence(
        session,
        user_id=user_id,
        skill=skill,
        kind=EvidenceKind.EMPLOYER_ASSESSMENT,
        source_type="employer",
        source_id=uuid.uuid4().hex,
    )
    created = (
        await client.post(
            PROJECTS, json={"title": "Jadval", "skills": ["Excel"]}, headers=_headers(user_id)
        )
    ).json()
    await client.delete(f"{PROJECTS}/{created['id']}", headers=_headers(user_id))

    record = await session.scalar(select(UserSkill).where(UserSkill.user_id == user_id))
    assert record.status == SkillStatus.VERIFIED


@pytest.mark.asyncio
async def test_editing_her_profile_skills_does_not_erase_project_evidence(client, session, user_id):
    """`sync_self_reported` withdraws what the *profile* claimed — nothing else."""
    await _woman(session, user_id)
    await _skill(session, "react", "React")
    await client.post(
        PROJECTS, json={"title": "Sayt", "skills": ["React"]}, headers=_headers(user_id)
    )

    await skills.sync_self_reported(session, user_id, [])

    evidence = await session.scalar(
        select(SkillEvidence).where(SkillEvidence.source_type == "portfolio_project")
    )
    assert evidence.revoked_at is None


@pytest.mark.asyncio
async def test_removing_a_skill_from_a_project_withdraws_only_that_claim(client, session, user_id):
    await _woman(session, user_id)
    await _skill(session, "react", "React")
    await _skill(session, "excel", "Excel")
    created = (
        await client.post(
            PROJECTS,
            json={"title": "Sayt", "skills": ["React", "Excel"]},
            headers=_headers(user_id),
        )
    ).json()

    await client.patch(
        f"{PROJECTS}/{created['id']}", json={"skills": ["React"]}, headers=_headers(user_id)
    )

    rows = await session.execute(
        select(Skill.slug, SkillEvidence.revoked_at)
        .join(UserSkill, UserSkill.skill_id == Skill.id)
        .join(SkillEvidence, SkillEvidence.user_skill_id == UserSkill.id)
        .where(SkillEvidence.source_type == "portfolio_project")
    )
    state = {slug: revoked is None for slug, revoked in rows.all()}
    assert state == {"react": True, "excel": False}


# --- overview -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_portfolio_counts_zero_everywhere(client, session, user_id):
    await _woman(session, user_id)

    body = (await client.get(ME, headers=_headers(user_id))).json()

    assert body["overview"] == {
        "achievements": 0,
        "certificates": 0,
        "learned": 0,
        "assessed": 0,
        "verified": 0,
        "practice_passed": 0,
        "practice_submitted": 0,
        "projects": 0,
    }
    assert body["achievements"] == []


@pytest.mark.asyncio
async def test_started_work_is_not_practice_and_waiting_work_is_not_passed(
    client, session, user_id
):
    await _woman(session, user_id)
    task = await _task(session, slug="ish", practises=[])
    other = await _task(session, slug="kutmoqda", practises=[])
    session.add_all(
        [
            TaskAttempt(task_id=task.id, user_id=user_id, attempt_no=1, status=TaskStatus.STARTED),
            TaskAttempt(
                task_id=other.id,
                user_id=user_id,
                attempt_no=1,
                status=TaskStatus.SUBMITTED,
                submitted_at=datetime.now(UTC),
            ),
        ]
    )
    await session.flush()

    body = (await client.get(ME, headers=_headers(user_id))).json()

    assert [p["task_slug"] for p in body["practice"]] == ["kutmoqda"]
    assert body["practice"][0]["status"] == "submitted"
    assert body["overview"]["practice_passed"] == 0
    assert body["overview"]["practice_submitted"] == 1


@pytest.mark.asyncio
async def test_enrollment_status_is_read_not_rewritten(session, user_id):
    """The portfolio is a reader. An enrollment in progress stays in progress."""
    await _woman(session, user_id)
    program = await _course(session, taught=["Excel"])
    enrollment = await learning.enroll(session, user_id=user_id, program_id=program.id)

    await portfolio.portfolio_for(session, user_id)

    await session.refresh(enrollment)
    assert enrollment.status == EnrollmentStatus.ENROLLED
