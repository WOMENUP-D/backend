"""Events: a listing with a time, read by date, recommended for real reasons,
registered for under the listing's own consent rules, and reminded of in-app.

Every event here is published through the organisation workspace or a partner
sync, the only two ways a real event enters WomanUP.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.constants import NotificationTrigger, OpportunitySource, SkillStatus
from app.models.assessment import DevelopmentScore
from app.models.notification import Notification
from app.models.opportunity import Opportunity
from app.services import ai_coach, events
from tests.api.test_organizations_api import (
    ADMIN,
    API,
    ORGS,
    _count,
    _hold,
    _org,
    _person,
    _skill,
    _token,
)

EVENTS = f"{API}/events"


def _at(**delta) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


async def _event(client, org_id, headers, **overrides) -> dict:
    payload = {
        "type": "workshop",
        "title_i18n": {"uz": "Moliyaviy savodxonlik ustaxonasi", "en": "Money workshop"},
        "skills": [],
        "starts_at": _at(days=10),
        "ends_at": _at(days=10, hours=3),
        "format": "offline",
        "venue": "Samarqand, Registon koʻchasi 5",
        "region": "samarkand",
        **overrides,
    }
    response = await client.post(f"{ORGS}/{org_id}/listings", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _publish_error(client, org_id, headers, **payload) -> str:
    body = {"title_i18n": {"uz": "Tadbir"}, **payload}
    response = await client.post(f"{ORGS}/{org_id}/listings", json=body, headers=headers)
    assert response.status_code == 422, response.text
    return response.json()["detail"]["reason"]


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_event_has_a_real_time_and_registration_closes_before_it(client, session):
    org_id, _, headers, _ = await _org(client, session)

    assert await _publish_error(client, org_id, headers, type="seminar") == "starts_required"
    assert (
        await _publish_error(client, org_id, headers, type="seminar", starts_at=_at(days=-1))
        == "starts_past"
    )
    assert (
        await _publish_error(
            client, org_id, headers, type="forum", starts_at=_at(days=5), ends_at=_at(days=4)
        )
        == "ends_before_start"
    )
    assert (
        await _publish_error(
            client, org_id, headers, type="forum", starts_at=_at(days=5), deadline=_at(days=6)
        )
        == "deadline_after_start"
    )
    assert (
        await _publish_error(client, org_id, headers, type="vacancy", starts_at=_at(days=5))
        == "not_an_event"
    )

    # A competition without a date is a standing listing, not an event.
    standing = await _event(
        client, org_id, headers, type="competition", starts_at=None, ends_at=None
    )
    assert standing["starts_at"] is None and standing["format"] is None
    dated = await _event(client, org_id, headers, type="competition")
    assert dated["starts_at"] is not None and dated["format"] == "offline"


@pytest.mark.asyncio
async def test_events_have_their_own_catalogue_and_stay_out_of_the_jobs_one(client, session):
    org_id, _, headers, _ = await _org(client, session)
    event = await _event(client, org_id, headers)
    await client.post(
        f"{ORGS}/{org_id}/listings",
        json={
            "type": "vacancy",
            "title_i18n": {"uz": "Kassir"},
            "deadline": _at(days=20),
        },
        headers=headers,
    )

    listed = (await client.get(EVENTS)).json()
    assert [item["id"] for item in listed["items"]] == [event["id"]]
    card = listed["items"][0]
    assert card["format"] == "offline" and card["venue"].startswith("Samarqand")
    assert card["organization"]["name"] == "Alfa Savdo MChJ"
    assert listed["signed_in"] is False and card["reasons"] == []

    jobs = (await client.get(f"{API}/opportunities/discover")).json()
    assert event["id"] not in {item["id"] for item in jobs["items"]}
    assert jobs["total"] == 1
    public = (await client.get(f"{API}/opportunities?size=50")).json()
    assert event["id"] not in {item["id"] for item in public["items"]}

    detail = (await client.get(f"{EVENTS}/{event['id']}")).json()
    assert detail["registration"] == "platform"
    assert detail["sharing"]["organization"]["name"] == "Alfa Savdo MChJ"
    # A job listing is not an event.
    vacancy_id = next(item["id"] for item in jobs["items"])
    assert (await client.get(f"{EVENTS}/{vacancy_id}")).status_code == 404


@pytest.mark.asyncio
async def test_filters_and_facets_only_offer_what_exists(client, session):
    org_id, _, headers, _ = await _org(client, session)
    await _skill(session, "budgeting-ev", "Budget", "budget")
    await _skill(session, "pitching-ev", "Pitch", "pitch")
    online = await _event(
        client, org_id, headers, format="online", venue=None, region=None, skills=["budget"]
    )
    later = await _event(
        client,
        org_id,
        headers,
        type="seminar",
        starts_at=_at(days=40),
        ends_at=None,
        skills=["pitch"],
    )

    by_format = (await client.get(f"{EVENTS}?format=online")).json()
    assert [item["id"] for item in by_format["items"]] == [online["id"]]
    # Counted with the other filters applied — both formats are still offered.
    assert by_format["facets"]["formats"] == {"online": 1, "offline": 1}
    assert by_format["facets"]["types"] == {"workshop": 1}

    by_topic = (await client.get(f"{EVENTS}?topic=pitch")).json()
    assert [item["id"] for item in by_topic["items"]] == [later["id"]]
    topics = {facet["skill"]["slug"] for facet in by_topic["facets"]["topics"]}
    assert topics == {"budgeting-ev", "pitching-ev"}

    window = {
        "from": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "to": (datetime.now(UTC) + timedelta(days=60)).isoformat(),
    }
    month = (await client.get(EVENTS, params=window)).json()
    assert [item["id"] for item in month["items"]] == [later["id"]]


@pytest.mark.asyncio
async def test_an_event_that_began_takes_no_registrations_and_one_that_ended_is_gone(
    client, session
):
    org_id, _, headers, _ = await _org(client, session)
    running = await _event(client, org_id, headers)
    over = await _event(client, org_id, headers)
    row = await session.get(Opportunity, uuid.UUID(running["id"]))
    row.starts_at = datetime.now(UTC) - timedelta(hours=1)
    row.ends_at = datetime.now(UTC) + timedelta(hours=2)
    ended = await session.get(Opportunity, uuid.UUID(over["id"]))
    ended.starts_at = datetime.now(UTC) - timedelta(days=2)
    ended.ends_at = datetime.now(UTC) - timedelta(days=1)
    await session.flush()

    listed = (await client.get(EVENTS)).json()["items"]
    assert [item["id"] for item in listed] == [running["id"]]
    assert listed[0]["happening"] is True and listed[0]["is_open"] is False

    woman = await _person(session)
    refused = await client.post(
        f"{API}/opportunities/{running['id']}/apply", json={"consent": True}, headers=_token(woman)
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "closed"


# ---------------------------------------------------------------------------
# Her side: registering, saving, reminders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registering_is_applying_with_consent_to_that_organiser_only(client, session):
    org_id, _, headers, _ = await _org(client, session)
    event = await _event(client, org_id, headers)
    woman = await _person(session)

    unconsented = await client.post(
        f"{API}/opportunities/{event['id']}/apply", json={"consent": False}, headers=_token(woman)
    )
    assert unconsented.status_code == 403
    registered = await client.post(
        f"{API}/opportunities/{event['id']}/apply", json={"consent": True}, headers=_token(woman)
    )
    assert registered.status_code == 201, registered.text

    [mine] = (await client.get(f"{EVENTS}/me", headers=_token(woman))).json()
    assert mine["id"] == event["id"] and mine["application_status"] == "submitted"
    # The organiser sees who registered, in its own workspace, and nothing more.
    [row] = (await client.get(f"{ORGS}/{org_id}/applications", headers=headers)).json()
    assert row["profile"]["first_name"] == "Malika"
    assert "phone" not in row["profile"] and "email" not in row["profile"]


@pytest.mark.asyncio
async def test_saved_events_are_hers_and_a_visitor_has_none(client, session):
    org_id, _, headers, _ = await _org(client, session)
    event = await _event(client, org_id, headers)
    other = await _event(client, org_id, headers, starts_at=_at(days=12), ends_at=None)
    woman = await _person(session)

    saved = await client.put(f"{API}/opportunities/{event['id']}/save", headers=_token(woman))
    assert saved.status_code == 204
    mine = (await client.get(f"{EVENTS}?mine=true", headers=_token(woman))).json()
    assert [item["id"] for item in mine["items"]] == [event["id"]]
    assert mine["items"][0]["saved"] is True
    assert other["id"] not in {item["id"] for item in mine["items"]}
    assert (await client.get(f"{EVENTS}?mine=true")).status_code == 401


@pytest.mark.asyncio
async def test_a_reminder_is_in_app_the_day_before_and_hidden_until_it_is_due(client, session):
    org_id, _, headers, _ = await _org(client, session)
    event = await _event(client, org_id, headers)
    woman = await _person(session)
    starts = datetime.fromisoformat(event["starts_at"])

    set_ = await client.put(f"{EVENTS}/{event['id']}/reminder", headers=_token(woman))
    assert set_.status_code == 200, set_.text
    due = datetime.fromisoformat(set_.json()["remind_at"])
    assert abs((starts - due) - timedelta(hours=24)) < timedelta(seconds=1)

    [note] = (
        await session.scalars(select(Notification).where(Notification.user_id == woman))
    ).all()
    assert note.channel.value == "in_app"
    assert note.trigger == NotificationTrigger.EVENT_REMINDER
    assert note.title == "Ertaga: Moliyaviy savodxonlik ustaxonasi"
    assert "Registon" in note.body and note.action_url == f"/tadbirlar/{event['id']}"

    # Scheduled for tomorrow, so not shown or counted today.
    assert (await client.get(f"{API}/notifications", headers=_token(woman))).json() == []
    count = (await client.get(f"{API}/notifications/unread-count", headers=_token(woman))).json()
    assert count == {"unread": 0}

    card = (await client.get(f"{EVENTS}/{event['id']}", headers=_token(woman))).json()
    assert card["reminder_at"] is not None
    # Asking again replaces it; cancelling removes it.
    await client.put(f"{EVENTS}/{event['id']}/reminder", headers=_token(woman))
    assert await _count(session, Notification, user_id=woman) == 1
    gone = await client.delete(f"{EVENTS}/{event['id']}/reminder", headers=_token(woman))
    assert gone.status_code == 204
    assert await _count(session, Notification, user_id=woman) == 0


@pytest.mark.asyncio
async def test_a_reminder_comes_an_hour_before_when_sooner_and_never_too_late(client, session):
    org_id, _, headers, _ = await _org(client, session)
    soon = await _event(client, org_id, headers, starts_at=_at(hours=5), ends_at=None)
    imminent = await _event(client, org_id, headers, starts_at=_at(hours=2), ends_at=None)
    row = await session.get(Opportunity, uuid.UUID(imminent["id"]))
    row.starts_at = datetime.now(UTC) + timedelta(minutes=30)
    await session.flush()
    woman = await _person(session)

    set_ = await client.put(f"{EVENTS}/{soon['id']}/reminder", headers=_token(woman))
    note = await session.scalar(select(Notification).where(Notification.user_id == woman))
    assert set_.status_code == 200 and note.title.startswith("Bir soatdan keyin:")
    late = await client.put(f"{EVENTS}/{imminent['id']}/reminder", headers=_token(woman))
    assert late.status_code == 409 and late.json()["detail"]["reason"] == "too_soon"


@pytest.mark.asyncio
async def test_a_due_reminder_appears_in_her_notifications(client, session):
    woman = await _person(session)
    session.add(
        Notification(
            user_id=woman,
            trigger=NotificationTrigger.EVENT_REMINDER,
            title="Ertaga: Forum",
            body="20-oktabr, 10:00",
            scheduled_for=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.flush()
    [shown] = (await client.get(f"{API}/notifications", headers=_token(woman))).json()
    assert shown["title"] == "Ertaga: Forum"
    count = (await client.get(f"{API}/notifications/unread-count", headers=_token(woman))).json()
    assert count == {"unread": 1}


@pytest.mark.asyncio
async def test_moving_an_event_moves_her_reminder_and_taking_it_down_drops_it(client, session):
    org_id, _, headers, admin = await _org(client, session)
    event = await _event(client, org_id, headers)
    woman = await _person(session)
    await client.put(f"{EVENTS}/{event['id']}/reminder", headers=_token(woman))

    later = datetime.now(UTC) + timedelta(days=15)
    moved = await client.patch(
        f"{ORGS}/{org_id}/listings/{event['id']}",
        json={"starts_at": later.isoformat(), "ends_at": None},
        headers=headers,
    )
    assert moved.status_code == 200, moved.text
    note = await session.scalar(select(Notification).where(Notification.user_id == woman))
    assert abs(note.scheduled_for - (later - timedelta(hours=24))) < timedelta(seconds=1)

    suspended = await client.patch(f"{ADMIN}/{org_id}", json={"is_active": False}, headers=admin)
    assert suspended.status_code == 200
    assert await _count(session, Notification, user_id=woman) == 0
    assert (await client.get(f"{EVENTS}/{event['id']}")).status_code == 404
    assert (await client.get(EVENTS)).json()["items"] == []


@pytest.mark.asyncio
async def test_a_girl_is_only_offered_events_open_to_her_age(client, session):
    org_id, _, headers, _ = await _org(client, session)
    skill = await _skill(session, "coding-ev", "Coding", "coding")
    adults = await _event(client, org_id, headers, skills=["coding"])
    for_girls = await _event(
        client, org_id, headers, skills=["coding"], eligibility={"age_min": 12, "age_max": 17}
    )
    girl = await _person(session, born=date(datetime.now().year - 14, 1, 1))
    await _hold(session, girl, skill, SkillStatus.LEARNED)

    recommended = (await client.get(f"{EVENTS}/recommended", headers=_token(girl))).json()
    assert [item["id"] for item in recommended] == [for_girls["id"]]
    refused = await client.put(f"{EVENTS}/{adults['id']}/reminder", headers=_token(girl))
    assert refused.status_code == 403 and refused.json()["detail"]["reason"] == "not_eligible"
    allowed = await client.put(f"{EVENTS}/{for_girls['id']}/reminder", headers=_token(girl))
    assert allowed.status_code == 200


# ---------------------------------------------------------------------------
# Why it is for her
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recommendations_carry_real_reasons_and_region_alone_is_not_one(client, session):
    org_id, _, headers, _ = await _org(client, session)
    budget = await _skill(session, "budget-rec", "Budget planning", "budget planning")
    budget.dimensions = ["financial_literacy"]
    await _skill(session, "pottery-rec", "Pottery", "pottery")
    await session.flush()
    covered = await _event(client, org_id, headers, skills=["budget planning"])
    nearby = await _event(client, org_id, headers, skills=["pottery"])

    woman = await _person(session, interests=("budget planning",))
    await _hold(session, woman, budget, SkillStatus.LEARNED)
    session.add(
        DevelopmentScore(user_id=woman, dimension="financial_literacy", baseline=35, current=35)
    )
    await session.flush()

    [item] = (await client.get(f"{EVENTS}/recommended", headers=_token(woman))).json()
    assert item["id"] == covered["id"]
    kinds = [reason["kind"] for reason in item["reasons"]]
    assert kinds == ["skill", "interest", "score", "region"]
    score = next(reason for reason in item["reasons"] if reason["kind"] == "score")
    assert (score["dimension"], score["score"]) == ("financial_literacy", 35)
    # The pottery evening is in her region and says nothing else about her.
    assert nearby["id"] not in {card["id"] for card in [item]}

    stranger = await _person(session, region=None)
    assert (await client.get(f"{EVENTS}/recommended", headers=_token(stranger))).json() == []


# ---------------------------------------------------------------------------
# The Coach and her week
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_coach_names_only_real_events_and_states_them_as_recorded(client, session):
    org_id, _, headers, _ = await _org(client, session)
    budget = await _skill(session, "budget-coach", "Budget", "budget")
    event = await _event(client, org_id, headers, skills=["budget"], format="online", venue=None)
    woman = await _person(session)
    await _hold(session, woman, budget, SkillStatus.LEARNED)

    context = await ai_coach.build(session, woman, language="en")
    assert context.offer[event["id"]].kind == "event"
    assert any("events worth her time" in line for line in context.context_lines)
    assert any(s.key == "coach.q.events" for s in context.read.suggestions)

    focused = await ai_coach.build(
        session, woman, language="en", opportunity_id=uuid.UUID(event["id"])
    )
    lines = "\n".join(focused.context_lines)
    assert "the event she is asking about" in lines
    assert "where: online" in lines and "registration: through WomanUP" in lines
    assert "(Tashkent)" in lines


@pytest.mark.asyncio
async def test_her_week_is_real_records_or_nothing(client, session):
    newcomer = await _person(session)
    empty = (await client.get(f"{API}/ai/week", headers=_token(newcomer))).json()
    assert empty == {
        "lesson": None,
        "event": None,
        "event_why": None,
        "task": None,
        "opportunity": None,
    }

    org_id, _, headers, _ = await _org(client, session)
    event = await _event(client, org_id, headers)
    await client.put(f"{API}/opportunities/{event['id']}/save", headers=_token(newcomer))
    week = (await client.get(f"{API}/ai/week", headers=_token(newcomer))).json()
    assert week["event"]["id"] == event["id"] and week["event_why"] == "yours"


# ---------------------------------------------------------------------------
# Partner feeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_partner_feed_sends_listings_and_events_in_their_language(
    client, session, monkeypatch
):
    monkeypatch.setattr(settings, "edu_job_client_id", "edu-test")
    url = f"{API}/integrations/edu_job/opportunities/sync"
    batch = [
        {"external_id": "v-1", "type": "vacancy", "title": "Бухгалтер", "language": "ru"},
        {
            "external_id": "e-1",
            "type": "forum",
            "title_i18n": {"uz": "Ayollar forumi", "en": "Women's forum"},
            "starts_at": _at(days=9),
            "ends_at": _at(days=9, hours=6),
            "format": "offline",
            "venue": "Toshkent, Xalqlar doʻstligi saroyi",
            "url": "https://example.org/forum",
        },
        {"external_id": "x-1", "type": "raffle", "title": "?"},
        {"external_id": "x-2", "type": "vacancy"},
    ]
    assert (await client.post(url, json=batch)).status_code == 401
    sent = await client.post(url, json=batch, headers={"X-Client-Id": "edu-test"})
    assert sent.status_code == 200, sent.text
    assert sent.json()["detail"] == "2 created, 0 updated, 2 skipped"

    vacancy = await session.scalar(
        select(Opportunity).where(
            Opportunity.source == OpportunitySource.EDU_JOB, Opportunity.external_id == "v-1"
        )
    )
    assert vacancy.title_i18n == {"ru": "Бухгалтер"} and vacancy.starts_at is None
    [forum] = (await client.get(EVENTS)).json()["items"]
    assert forum["title_i18n"]["en"] == "Women's forum" and forum["format"] == "offline"
    detail = (await client.get(f"{EVENTS}/{forum['id']}")).json()
    assert detail["registration"] == "external"

    woman = await _person(session)
    await client.put(f"{EVENTS}/{forum['id']}/reminder", headers=_token(woman))
    batch[1]["is_active"] = False
    resent = await client.post(url, json=batch[:2], headers={"X-Client-Id": "edu-test"})
    assert resent.json()["detail"] == "0 created, 2 updated, 0 skipped"
    assert await _count(session, Notification, user_id=woman) == 0
    assert (await client.get(EVENTS)).json()["items"] == []


def test_reminder_words_come_from_the_event_in_her_language():
    item = Opportunity(
        title_i18n={"uz": "Forum", "ru": "Форум"},
        starts_at=datetime(2026, 10, 20, 5, 0, tzinfo=UTC),
        venue="Toshkent",
    )
    due = item.starts_at - events.REMIND_BEFORE
    assert events.reminder_text(item, due, "ru") == (
        "Завтра: Форум",
        "20 октября, 10:00 — Toshkent",
    )
    assert events.reminder_text(item, due, "en") == (
        "Tomorrow: Forum",
        "20 October, 10:00 — Toshkent",
    )
    late = item.starts_at - events.REMIND_LATE
    assert events.reminder_text(item, late, "uz")[0] == "Bir soatdan keyin: Forum"


@pytest.mark.asyncio
async def test_a_hyphenated_slug_resolves_on_its_own(session):
    """Listings store canonical slugs; "customer-service" must find its skill
    even when no alias spells it that way."""
    from app.models.skill import Skill
    from app.services.skills import SkillIndex

    session.add(
        Skill(
            slug="customer-service",
            name_i18n={"en": "Customer service"},
            category="professional",
            dimensions=[],
            aliases=["working with clients"],
            is_curated=True,
        )
    )
    await session.flush()
    index = await SkillIndex.load(session, ["customer-service"])
    assert index.ref("customer-service").slug == "customer-service"


@pytest.mark.asyncio
async def test_a_write_is_committed_before_its_answer_is_sent():
    """The session dependency ends with the endpoint, not after the response:
    a client that creates something and reads it straight back must find it."""
    from app.api.deps import DbSession

    dependency = DbSession.__metadata__[0]
    assert dependency.scope == "function"
