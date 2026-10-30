"""The collection endpoints: open to anonymous visitors, so they accept a fixed
vocabulary and nothing else, hand out one first-party cookie, keep no IP or
user agent, and refuse to be used as a free-text log."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.api import analytics as analytics_api
from app.core.constants import Role
from app.core.security import create_token
from app.models.analytics import AnalyticsEvent, PageView, VisitorPresence
from app.models.user import User, UserRole

API = "/api/v1/analytics"
PHONE_UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Mobile Safari/537.36"
)


@pytest.fixture(autouse=True)
def fresh_throttles():
    analytics_api._views = analytics_api.Throttle(analytics_api.MIN_SECONDS_BETWEEN_VIEWS)
    analytics_api._events = analytics_api.Throttle(
        analytics_api.MIN_SECONDS_BETWEEN_EVENTS, analytics_api.MAX_EVENTS_PER_MINUTE
    )
    analytics_api._heartbeats = analytics_api.Throttle(analytics_api.MIN_SECONDS_BETWEEN_HEARTBEATS)


async def _staff(session, role=Role.ADMIN):
    user = User(email=f"{uuid.uuid4().hex[:8]}@womanup.uz", phone=None)
    session.add(user)
    await session.flush()
    session.add(UserRole(user_id=user.id, role=role))
    await session.flush()
    token = create_token(user.id, role, "access", {"roles": [role.value]})
    return user, {"Authorization": f"Bearer {token}"}


async def test_a_view_is_recorded_with_a_cookie_and_three_words_from_the_user_agent(
    client, session
):
    response = await client.post(
        f"{API}/view",
        json={
            "path": "/dasturlar/0bdd1f67-5d4c-4436-9d55-2df4b02ea482?search=secret",
            "locale": "uz",
            "session": "sess_abcdef123456",
            "entry": True,
            "referrer": "https://t.me/womanup_uz/42?x=1",
        },
        headers={"User-Agent": PHONE_UA},
    )
    assert response.status_code == 200
    assert "wu_vid" in response.cookies

    view = (await session.execute(select(PageView))).scalar_one()
    assert view.path == "/dasturlar/:id"
    assert "secret" not in view.path
    assert view.session_id == "sess_abcdef123456"
    assert view.is_entry is True
    assert view.referrer_host == "t.me"
    assert (view.device, view.browser, view.os) == ("mobile", "Chrome", "Android")
    assert view.is_authenticated is False and view.is_staff is False
    # The raw cookie never reaches the table.
    assert response.cookies["wu_vid"] not in view.visitor_hash

    presence = (await session.execute(select(VisitorPresence))).scalar_one()
    assert presence.visitor_hash == view.visitor_hash
    assert presence.path == "/dasturlar/:id"


async def test_the_same_cookie_is_the_same_visitor(client, session):
    first = await client.post(f"{API}/view", json={"path": "/", "session": "sess_abcdef123456"})
    cookie = first.cookies["wu_vid"]
    analytics_api._views = analytics_api.Throttle(0)
    await client.post(
        f"{API}/view",
        json={"path": "/dasturlar", "session": "sess_abcdef123456"},
        cookies={"wu_vid": cookie},
    )
    hashes = {row.visitor_hash for row in (await session.execute(select(PageView))).scalars()}
    assert len(hashes) == 1
    assert (await session.scalar(select(VisitorPresence.visitor_hash))) in hashes


async def test_a_second_view_within_a_second_is_dropped(client, session):
    first = await client.post(f"{API}/view", json={"path": "/"})
    second = await client.post(
        f"{API}/view", json={"path": "/dasturlar"}, cookies={"wu_vid": first.cookies["wu_vid"]}
    )
    assert second.json()["detail"] == "throttled"
    assert len((await session.execute(select(PageView))).scalars().all()) == 1


async def test_a_signed_in_staff_view_is_marked_not_dropped(client, session):
    _, headers = await _staff(session, Role.MODERATOR)
    await client.post(f"{API}/view", json={"path": "/admin"}, headers=headers)
    view = (await session.execute(select(PageView))).scalar_one()
    assert view.is_authenticated is True
    assert view.is_staff is True


async def test_a_bot_is_counted_as_a_view_but_never_as_present(client, session):
    await client.post(
        f"{API}/view",
        json={"path": "/"},
        headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"},
    )
    assert (await session.scalar(select(PageView.device))) == "bot"
    assert (await session.execute(select(VisitorPresence))).first() is None


async def test_a_known_client_event_is_recorded_with_its_entity(client, session):
    program_id = str(uuid.uuid4())
    response = await client.post(
        f"{API}/event",
        json={
            "name": "program_viewed",
            "path": f"/dasturlar/{program_id}",
            "entity_type": "program",
            "entity_id": program_id,
            "properties": {"slug": "python", "answer": "never stored"},
        },
    )
    assert response.status_code == 200
    event = (await session.execute(select(AnalyticsEvent))).scalar_one()
    assert event.name == "program_viewed"
    assert event.source == "client"
    assert event.entity_type == "program" and event.entity_id == program_id
    assert event.path == "/dasturlar/:id"
    assert event.properties == {"slug": "python"}
    assert event.visitor_hash is not None


@pytest.mark.parametrize(
    "name", ["registration_completed", "program_completed", "made_up_event", "api_error"]
)
async def test_a_browser_cannot_report_an_outcome_or_an_unknown_event(client, session, name):
    response = await client.post(f"{API}/event", json={"name": name})
    assert response.status_code == 422
    assert (await session.execute(select(AnalyticsEvent))).first() is None


async def test_a_malformed_entity_is_refused(client):
    response = await client.post(
        f"{API}/event",
        json={"name": "program_viewed", "entity_type": "program", "entity_id": "<script>"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {"reason": "invalid_entity"}


async def test_a_malformed_body_is_a_validation_error_not_a_crash(client):
    response = await client.post(f"{API}/event", json={"name": 42, "properties": "x"})
    assert response.status_code == 422


async def test_a_heartbeat_refreshes_presence_and_writes_nothing_else(client, session):
    response = await client.post(f"{API}/heartbeat", json={"path": "/kabinet"})
    assert response.status_code == 200
    assert (await session.execute(select(VisitorPresence))).scalar_one().path == "/kabinet"
    assert (await session.execute(select(PageView))).first() is None
    assert (await session.execute(select(AnalyticsEvent))).first() is None

    again = await client.post(
        f"{API}/heartbeat", json={"path": "/reja"}, cookies={"wu_vid": response.cookies["wu_vid"]}
    )
    assert again.json()["detail"] == "throttled"
