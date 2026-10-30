"""Business events are written beside the records they describe — once.

The dashboard's counts come from the records; these events feed the funnel's
"started" steps and the activity timeline. What is pinned here is that each
real action leaves exactly one row, that a retry leaves none, that the visitor
cookie is linked when the browser sent it, and that recording never breaks the
action itself.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text

from app.api import auth as auth_api
from app.core.constants import Language, ProgramCategory, ProgramFormat, Role
from app.core.security import create_token
from app.models.analytics import AnalyticsEvent
from app.models.program import Program, ProgramModule
from app.models.user import User
from app.services.traffic import hash_visitor


@pytest.fixture(autouse=True)
def fresh_throttles():
    auth_api._login_throttle = auth_api.IpThrottle(limit=8)
    auth_api._register_throttle = auth_api.IpThrottle(limit=6, window=300.0)


async def _events(session, name: str) -> list[AnalyticsEvent]:
    rows = await session.execute(select(AnalyticsEvent).where(AnalyticsEvent.name == name))
    return list(rows.scalars())


async def test_registering_leaves_one_event_linked_to_the_visitor(client, session):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "dilnoza@example.com",
            "password": "olma-daraxti-2026",
            "accepted_privacy_policy": True,
        },
        cookies={"wu_vid": "visitor-cookie-value"},
        headers={"X-WU-Session": "sess_abcdef123456"},
    )
    assert response.status_code == 201
    user = await session.scalar(select(User).where(User.email == "dilnoza@example.com"))

    (event,) = await _events(session, "registration_completed")
    assert event.user_id == user.id
    assert event.source == "server"
    assert event.visitor_hash == hash_visitor("visitor-cookie-value")
    assert "visitor-cookie-value" not in (event.visitor_hash or "")
    assert event.session_id == "sess_abcdef123456"
    assert event.properties == {"provider": "password"}
    assert event.dedupe_key == f"registration_completed:{user.id}"
    assert event.is_staff is False


async def test_signing_in_leaves_a_login_event(client, session):
    await client.post(
        "/api/v1/auth/register",
        json={
            "email": "dilnoza@example.com",
            "password": "olma-daraxti-2026",
            "accepted_privacy_policy": True,
        },
    )
    response = await client.post(
        "/api/v1/auth/login", json={"email": "dilnoza@example.com", "password": "olma-daraxti-2026"}
    )
    assert response.status_code == 200
    assert len(await _events(session, "login_completed")) == 1


async def test_a_wrong_password_leaves_no_event(client, session):
    await client.post(
        "/api/v1/auth/register",
        json={
            "email": "dilnoza@example.com",
            "password": "olma-daraxti-2026",
            "accepted_privacy_policy": True,
        },
    )
    response = await client.post(
        "/api/v1/auth/login", json={"email": "dilnoza@example.com", "password": "wrong-wrong"}
    )
    assert response.status_code == 401
    assert await _events(session, "login_completed") == []


async def _learner(session) -> tuple[uuid.UUID, dict[str, str]]:
    user = User(email=f"{uuid.uuid4().hex[:8]}@example.com")
    session.add(user)
    await session.flush()
    from app.models.user import UserRole

    session.add(UserRole(user_id=user.id, role=Role.USER))
    await session.flush()
    token = create_token(user.id, Role.USER, "access", {"roles": ["user"]})
    return user.id, {"Authorization": f"Bearer {token}"}


async def _course(session) -> tuple[Program, ProgramModule]:
    program = Program(
        slug="python",
        title_i18n={"uz": "Python", "ru": "Python", "en": "Python"},
        category=ProgramCategory.DIGITAL_SAFETY,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        is_published=True,
    )
    session.add(program)
    await session.flush()
    module = ProgramModule(program_id=program.id, order_index=0, title_i18n={"uz": "1"})
    session.add(module)
    await session.flush()
    return program, module


async def test_enrolling_twice_is_one_event(client, session):
    _, headers = await _learner(session)
    program, _ = await _course(session)
    first = await client.post(f"/api/v1/programs/{program.id}/enroll", headers=headers)
    second = await client.post(f"/api/v1/programs/{program.id}/enroll", headers=headers)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    (event,) = await _events(session, "program_enrolled")
    assert event.entity_type == "program" and event.entity_id == str(program.id)


async def test_finishing_the_last_module_records_the_lesson_and_the_course_once(client, session):
    _, headers = await _learner(session)
    program, module = await _course(session)
    enrollment = (
        await client.post(f"/api/v1/programs/{program.id}/enroll", headers=headers)
    ).json()
    body = {"module_id": str(module.id), "completed": True}
    for _ in range(2):
        response = await client.post(
            f"/api/v1/programs/enrollments/{enrollment['id']}/progress", headers=headers, json=body
        )
        assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert len(await _events(session, "lesson_completed")) == 1
    assert len(await _events(session, "program_completed")) == 1
    # Un-ticking is not an event.
    await client.post(
        f"/api/v1/programs/enrollments/{enrollment['id']}/progress",
        headers=headers,
        json={"module_id": str(module.id), "completed": False},
    )
    assert await session.scalar(select(func.count(AnalyticsEvent.id))) == 3  # + the enrollment


async def test_adding_a_cv_entry_records_one_event(client, session):
    _, headers = await _learner(session)
    response = await client.post(
        "/api/v1/users/me/experience",
        headers=headers,
        json={
            "organization": "Ipak Yoli",
            "position": "Buxgalter",
            "start_date": "2024-01-01",
            "client_ref": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 201, response.text
    (event,) = await _events(session, "cv_updated")
    assert event.entity_type == "work_experience"
    assert event.properties == {}  # nothing she typed is in the event


async def test_completing_onboarding_is_recorded_once(client, session):
    _, headers = await _learner(session)
    for _ in range(2):
        response = await client.post("/api/v1/users/me/onboarding/complete", headers=headers)
        assert response.status_code == 200
    assert len(await _events(session, "onboarding_completed")) == 1


async def test_a_failing_event_write_never_fails_the_action(client, session):
    """Analytics rides on the request; a broken table must not break sign-up.

    The table is dropped inside the test's own transaction, so the INSERT
    fails in the database itself — the case a savepoint exists for.
    """
    await session.execute(text("DROP TABLE analytics_events"))
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "dilnoza@example.com",
            "password": "olma-daraxti-2026",
            "accepted_privacy_policy": True,
        },
    )
    assert response.status_code == 201
    assert await session.scalar(select(User).where(User.email == "dilnoza@example.com"))
