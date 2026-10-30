"""The participant roster: one row per woman, individual-level, and therefore
held to stricter rules than the dashboards — identifiers for administrators
only, a region for a coordinator, every card and export audited."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.constants import (
    EnrollmentStatus,
    Language,
    ProgramCategory,
    ProgramFormat,
    Region,
    Role,
    ScoreDimension,
    UserStatus,
)
from app.core.security import create_token
from app.models.analytics import AnalyticsEvent, PageView
from app.models.assessment import Assessment, DevelopmentScore
from app.models.audit import AiInteraction, AuditLog
from app.models.career import WorkExperience
from app.models.plan import DevelopmentPlan
from app.models.profile import Profile
from app.models.program import Enrollment, Program
from app.models.user import User, UserRole

API = "/api/v1/admin/users"


def at(day: str, hour: int = 12) -> datetime:
    local = datetime.fromisoformat(day).replace(hour=hour)
    return (local - timedelta(hours=5)).replace(tzinfo=UTC)


def _token(user_id: uuid.UUID, *roles: Role) -> dict[str, str]:
    roles = roles or (Role.USER,)
    token = create_token(user_id, roles[0], "access", {"roles": [r.value for r in roles]})
    return {"Authorization": f"Bearer {token}"}


async def _person(
    session,
    *,
    name: str | None = None,
    email: str | None = None,
    region: Region | None = Region.SAMARKAND,
    roles=(Role.USER,),
    status: UserStatus = UserStatus.ACTIVE,
    created: datetime | None = None,
    last_active: datetime | None = None,
    scope: Region | None = None,
) -> uuid.UUID:
    user = User(
        email=email or f"{uuid.uuid4().hex[:8]}@example.com",
        language=Language.UZ,
        region=region,
        status=status,
        created_at=created or at("2026-03-01"),
        last_active_at=last_active,
    )
    session.add(user)
    await session.flush()
    for role in roles:
        session.add(
            UserRole(
                user_id=user.id,
                role=role,
                scope_region=scope if role == Role.REGIONAL_COORDINATOR else None,
            )
        )
    session.add(Profile(user_id=user.id, full_name=name))
    await session.flush()
    return user.id


async def _program(session, slug: str = "python") -> Program:
    program = Program(
        slug=slug,
        title_i18n={"uz": f"Kurs {slug}", "ru": f"Курс {slug}", "en": f"Course {slug}"},
        category=ProgramCategory.DIGITAL_SAFETY,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        is_published=True,
    )
    session.add(program)
    await session.flush()
    return program


async def _busy_woman(session) -> uuid.UUID:
    """Dilnoza: three days of use, a score, a finished course, a plan, a CV line."""
    user_id = await _person(
        session,
        name="Dilnoza Karimova",
        email="dilnoza@example.com",
        last_active=at("2026-03-20"),
    )
    program = await _program(session)
    assessment = Assessment(user_id=user_id, completed_at=at("2026-03-02"), overall_score=61)
    session.add(assessment)
    await session.flush()
    session.add_all(
        [
            DevelopmentScore(
                user_id=user_id,
                assessment_id=assessment.id,
                dimension=ScoreDimension.EDUCATION_SKILLS,
                baseline=50,
                current=70,
                target=80,
                measured_at=at("2026-03-02"),
            ),
            DevelopmentScore(
                user_id=user_id,
                assessment_id=assessment.id,
                dimension=ScoreDimension.DIGITAL_SKILLS,
                baseline=40,
                current=50,
                target=70,
                measured_at=at("2026-03-02"),
            ),
            Enrollment(
                user_id=user_id,
                program_id=program.id,
                status=EnrollmentStatus.COMPLETED,
                progress_percent=100,
                started_at=at("2026-03-03"),
                completed_at=at("2026-03-10"),
                last_activity_at=at("2026-03-10"),
            ),
            DevelopmentPlan(
                user_id=user_id, title="Reja", is_active=True, accepted_at=at("2026-03-04")
            ),
            WorkExperience(
                user_id=user_id,
                organization="Ipak Yoli",
                position="Buxgalter",
                start_date=datetime(2024, 1, 1).date(),
            ),
            AiInteraction(
                user_id=user_id,
                feature="assistant_education",
                model_version="m",
                prompt_version="p",
                trace_id="t1",
            ),
            PageView(
                visitor_hash="d",
                path="/kabinet",
                user_id=user_id,
                is_authenticated=True,
                occurred_at=at("2026-03-02"),
                session_id="s1",
            ),
            PageView(
                visitor_hash="d",
                path="/dasturlar",
                user_id=user_id,
                is_authenticated=True,
                occurred_at=at("2026-03-03"),
                session_id="s2",
            ),
            PageView(
                visitor_hash="d",
                path="/reja",
                user_id=user_id,
                is_authenticated=True,
                occurred_at=at("2026-03-03", 13),
                session_id="s2",
            ),
            AnalyticsEvent(
                name="program_completed",
                occurred_at=at("2026-03-10"),
                user_id=user_id,
                entity_type="program",
                entity_id=str(program.id),
            ),
            AnalyticsEvent(name="login_completed", occurred_at=at("2026-03-11"), user_id=user_id),
        ]
    )
    await session.flush()
    return user_id


async def _get(client, path: str, headers, **params):
    response = await client.get(f"{API}{path}", headers=headers, params=params)
    assert response.status_code == 200, (path, response.status_code, response.text)
    return response.json()


# --------------------------------------------------------------------- access


async def test_roster_and_card_need_a_staff_token(client, session):
    user_id = await _person(session)
    assert (await client.get(f"{API}/roster")).status_code == 401
    assert (await client.get(f"{API}/roster", headers=_token(user_id))).status_code == 403
    assert (await client.get(f"{API}/{user_id}/card", headers=_token(user_id))).status_code == 403
    assert (await client.get(f"{API}/roster/export", headers=_token(user_id))).status_code == 403


async def test_the_export_is_for_administrators_only(client, session):
    moderator = await _person(session, roles=(Role.MODERATOR,))
    response = await client.get(f"{API}/roster/export", headers=_token(moderator, Role.MODERATOR))
    assert response.status_code == 403


# ------------------------------------------------------------------- figures


async def test_an_administrator_sees_names_masked_addresses_and_every_figure(client, session):
    admin = await _person(session, roles=(Role.ADMIN,))
    dilnoza = await _busy_woman(session)
    await _person(session, name="Quiet Woman", last_active=None)

    body = await _get(client, "/roster", _token(admin, Role.ADMIN))
    assert body["total"] == 2
    assert body["scope"]["identifiers"] is True
    assert body["scope"]["views_since"] == "2026-03-02"
    row = next(item for item in body["items"] if item["id"] == str(dilnoza))
    assert row["name"] == "Dilnoza Karimova"
    assert row["email"] == "d******@example.com"
    assert "dilnoza@" not in row["email"]
    assert len(row["tag"]) == 6
    # Three days of views (2, 3 March) plus the diagnostic (2 March), the plan
    # (4 March), the course (10 March) and the CV line, assistant question and
    # plan draft written today: a day counts once however much happened on it.
    assert (row["views"], row["sessions"], row["active_days"]) == (3, 2, 5)
    assert row["score"] == 60.0
    assert row["diagnostics"] == 1 and row["last_diagnostic_at"]
    assert (row["courses_enrolled"], row["courses_completed"]) == (1, 1)
    assert row["plan_accepted"] is True
    assert row["ai_requests"] == 1 and row["cv_entries"] == 1 and row["applications"] == 0
    # Most recently active first.
    assert body["items"][0]["id"] == str(dilnoza)


async def test_a_moderator_sees_a_tag_and_no_identifiers(client, session):
    moderator = await _person(session, roles=(Role.MODERATOR,))
    dilnoza = await _busy_woman(session)
    body = await _get(client, "/roster", _token(moderator, Role.MODERATOR))
    assert body["scope"]["identifiers"] is False
    row = body["items"][0]
    assert row["id"] == str(dilnoza)
    assert row["name"] is None and row["email"] is None
    assert row["tag"] and row["score"] == 60.0
    assert "Dilnoza" not in str(body) and "dilnoza" not in str(body)


async def test_search_by_name_is_honoured_for_administrators_only(client, session):
    admin = await _person(session, roles=(Role.ADMIN,))
    moderator = await _person(session, roles=(Role.MODERATOR,))
    await _busy_woman(session)
    await _person(session, name="Gulnora Tosheva")
    found = await _get(client, "/roster", _token(admin, Role.ADMIN), search="dilnoza")
    assert found["total"] == 1 and found["items"][0]["name"] == "Dilnoza Karimova"
    ignored = await _get(client, "/roster", _token(moderator, Role.MODERATOR), search="dilnoza")
    assert ignored["total"] == 2


async def test_a_coordinator_sees_her_region_and_nobody_else(client, session):
    coordinator = await _person(
        session, roles=(Role.REGIONAL_COORDINATOR,), region=Region.BUKHARA, scope=Region.BUKHARA
    )
    await _busy_woman(session)  # Samarkand
    local = await _person(session, name="Local Woman", region=Region.BUKHARA)
    headers = _token(coordinator, Role.REGIONAL_COORDINATOR)
    body = await _get(client, "/roster", headers, region="samarkand")
    assert [item["id"] for item in body["items"]] == [str(local)]
    assert body["scope"]["region"] == "bukhara"
    assert body["items"][0]["name"] is None
    outside = await session.scalar(select(User.id).where(User.region == Region.SAMARKAND))
    assert (await client.get(f"{API}/{outside}/card", headers=headers)).status_code == 404


async def test_a_coordinator_without_a_region_is_refused(client, session):
    coordinator = await _person(session, roles=(Role.REGIONAL_COORDINATOR,), region=None)
    response = await client.get(
        f"{API}/roster", headers=_token(coordinator, Role.REGIONAL_COORDINATOR)
    )
    assert response.status_code == 403
    assert response.json()["detail"] == {"reason": "no_region_scope"}


async def test_staff_accounts_are_not_participants(client, session):
    admin = await _person(session, roles=(Role.ADMIN,), name="The Admin")
    await _person(session, roles=(Role.TRAINER,), name="A Trainer")
    await _person(session, name="A Participant")
    body = await _get(client, "/roster", _token(admin, Role.ADMIN))
    assert [item["name"] for item in body["items"]] == ["A Participant"]


async def test_a_deleted_account_keeps_its_row_but_loses_its_identifiers(client, session):
    admin = await _person(session, roles=(Role.ADMIN,))
    await _person(session, name="Gone Woman", status=UserStatus.DELETED)
    body = await _get(client, "/roster", _token(admin, Role.ADMIN))
    row = body["items"][0]
    assert row["status"] == "deleted" and row["name"] is None and row["email"] is None


async def test_sorting_and_paging_happen_on_the_server(client, session):
    admin = await _person(session, roles=(Role.ADMIN,))
    await _busy_woman(session)
    await _person(session, name="No Score")
    await _person(session, name="Also None")
    by_score = await _get(client, "/roster", _token(admin, Role.ADMIN), sort="score", order="desc")
    assert by_score["items"][0]["name"] == "Dilnoza Karimova"
    assert by_score["items"][-1]["score"] is None
    page = await _get(client, "/roster", _token(admin, Role.ADMIN), sort="joined", size=2, page=2)
    assert page["total"] == 3 and len(page["items"]) == 1 and page["page"] == 2


# ---------------------------------------------------------------------- card


async def test_the_card_carries_dimensions_courses_and_steps_and_is_audited(client, session):
    admin = await _person(session, roles=(Role.ADMIN,))
    dilnoza = await _busy_woman(session)
    body = await _get(client, f"/{dilnoza}/card", _token(admin, Role.ADMIN))
    assert body["row"]["name"] == "Dilnoza Karimova"
    dims = {d["dimension"]: d for d in body["dimensions"]}
    assert dims["education_skills"] == {
        "dimension": "education_skills",
        "baseline": 50.0,
        "current": 70.0,
        "target": 80.0,
    }
    (course,) = body["courses"]
    assert course["slug"] == "python" and course["status"] == "completed"
    assert course["progress_percent"] == 100 and course["certificate"] is False
    # Logins are not a story; the completed course is, with its title.
    assert [event["name"] for event in body["events"]] == ["program_completed"]
    assert body["events"][0]["title_i18n"]["uz"] == "Kurs python"

    entry = await session.scalar(select(AuditLog).where(AuditLog.action == "user.view"))
    assert entry is not None
    assert entry.entity_id == str(dilnoza) and entry.actor_id == admin
    assert entry.classification.value == "personal"


async def test_the_card_never_carries_answers_or_messages(client, session):
    admin = await _person(session, roles=(Role.ADMIN,))
    dilnoza = await _busy_woman(session)
    body = await _get(client, f"/{dilnoza}/card", _token(admin, Role.ADMIN))
    text = str(body)
    for private in ("prompt_excerpt", "response_excerpt", "raw_answer", "birth_date", "phone"):
        assert private not in text


# -------------------------------------------------------------------- export


async def test_the_export_is_the_table_with_identifiers_and_is_audited(client, session):
    admin = await _person(session, roles=(Role.ADMIN,))
    await _busy_woman(session)
    response = await client.get(f"{API}/roster/export", headers=_token(admin, Role.ADMIN))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("tag,name,email,region,status")
    assert "Dilnoza Karimova" in lines[1] and "d******@example.com" in lines[1]
    assert "dilnoza@example.com" not in response.text
    entry = await session.scalar(select(AuditLog).where(AuditLog.action == "users.export"))
    assert entry is not None and entry.changes["rows"] == 1
