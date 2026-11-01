"""The Analytics endpoints: staff-only, national, counted from records, and
honest about what has not been recorded.

Fixtures insert the rows a real journey leaves behind — views with and without
sessions, accounts, enrollments, assessments, events — at chosen moments, so
each test can say exactly what the answer must be.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

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
from app.models.analytics import AnalyticsEvent, PageView, VisitorPresence
from app.models.assessment import Assessment, DevelopmentScore
from app.models.program import Enrollment, Program
from app.models.user import User, UserRole
from app.services import insights

API = "/api/v1/admin/analytics"
ENDPOINTS = (
    "overview",
    "traffic",
    "accounts",
    "engagement",
    "funnel",
    "programs",
    "features",
    "activity",
    "errors",
    "live",
    "definitions",
)
MARCH = {"date_from": "2026-03-01", "date_to": "2026-03-31"}


def at(day: str, hour: int = 12) -> datetime:
    """A moment on a Tashkent calendar day, as the UTC time the database holds."""
    local = datetime.fromisoformat(day).replace(hour=hour)
    return (local - timedelta(hours=5)).replace(tzinfo=UTC)


def _token(user_id: uuid.UUID, *roles: Role) -> dict[str, str]:
    roles = roles or (Role.USER,)
    token = create_token(user_id, roles[0], "access", {"roles": [r.value for r in roles]})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def no_cache():
    insights.clear_cache()
    yield
    insights.clear_cache()


async def _person(
    session,
    *,
    created: datetime,
    roles=(Role.USER,),
    region: Region | None = Region.SAMARKAND,
    status: UserStatus = UserStatus.ACTIVE,
    onboarded: datetime | None = None,
    provider: str = "password",
) -> uuid.UUID:
    user = User(
        phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
        language=Language.UZ,
        region=region,
        status=status,
        created_at=created,
        onboarding_completed_at=onboarded,
        auth_provider=provider,
    )
    session.add(user)
    await session.flush()
    for role in roles:
        session.add(UserRole(user_id=user.id, role=role))
    await session.flush()
    return user.id


async def _staff(session, role: Role = Role.ADMIN) -> dict[str, str]:
    user_id = await _person(session, created=at("2026-01-01"), roles=(role,))
    return _token(user_id, role)


def _view(visitor: str, day: str, path: str = "/", hour: int = 12, **extra) -> PageView:
    return PageView(
        visitor_hash=visitor,
        path=path,
        occurred_at=at(day, hour),
        is_authenticated=extra.get("user_id") is not None,
        **extra,
    )


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


async def _get(client, endpoint: str, headers, **params):
    response = await client.get(f"{API}/{endpoint}", headers=headers, params=params)
    assert response.status_code == 200, (endpoint, response.status_code, response.text)
    return response.json()


def _figure(body: dict, key: str, field: str = "figures"):
    return next(item for item in body[field] if item["key"] == key)


# --------------------------------------------------------------------- access


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_every_endpoint_requires_a_token(client, endpoint):
    response = await client.get(f"{API}/{endpoint}")
    assert response.status_code == 401


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_a_participant_is_refused(client, session, endpoint):
    user_id = await _person(session, created=at("2026-01-01"))
    response = await client.get(f"{API}/{endpoint}", headers=_token(user_id))
    assert response.status_code == 403


@pytest.mark.parametrize("role", [Role.REGIONAL_COORDINATOR, Role.TRAINER, Role.PARTNER])
async def test_national_figures_are_not_for_regional_or_organisation_roles(client, session, role):
    headers = await _staff(session, role)
    response = await client.get(f"{API}/overview", headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize("role", [Role.ADMIN, Role.MODERATOR])
@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_administrators_and_moderators_read_every_endpoint(client, session, role, endpoint):
    """On an empty database too: nothing here may fail for lack of data."""
    headers = await _staff(session, role)
    await _get(client, endpoint, headers)


async def test_a_forged_role_in_a_token_does_not_open_the_door(client, session):
    user_id = await _person(session, created=at("2026-01-01"))
    token = create_token(user_id, Role.USER, "access", {"roles": ["admin"]})
    # The token is signed with the server's secret, so a forged *claim* here
    # is what an attacker could do only with the secret — then the roles are
    # believed. What must hold is that a learner's genuine token is refused.
    assert token
    response = await client.get(f"{API}/overview", headers=_token(user_id, Role.USER))
    assert response.status_code == 403


# ------------------------------------------------------------------ periods


async def test_a_reversed_period_is_refused(client, session):
    headers = await _staff(session)
    response = await client.get(
        f"{API}/traffic",
        headers=headers,
        params={"date_from": "2026-03-31", "date_to": "2026-03-01"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {"reason": "period_reversed"}


async def test_a_period_over_a_year_is_refused(client, session):
    headers = await _staff(session)
    response = await client.get(
        f"{API}/traffic",
        headers=headers,
        params={"date_from": "2024-01-01", "date_to": "2026-03-01"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {"reason": "period_too_long"}


async def test_no_period_means_the_last_thirty_days_in_tashkent(client, session):
    headers = await _staff(session)
    body = await _get(client, "traffic", headers)
    period = body["scope"]["period"]
    assert period["date_from"] and period["date_to"]
    first = datetime.fromisoformat(period["date_from"]).date()
    last = datetime.fromisoformat(period["date_to"]).date()
    assert (last - first).days == 29
    assert body["scope"]["timezone"] == "Asia/Tashkent"


# ------------------------------------------------------------------- traffic


async def test_traffic_counts_views_visitors_sessions_and_tells_new_from_returning(client, session):
    headers = await _staff(session)
    # "a" came in February and again in March: returning. "b" is new in March.
    session.add_all(
        [
            _view("a", "2026-02-20"),
            _view(
                "a",
                "2026-03-02",
                "/dasturlar",
                session_id="s1",
                is_entry=True,
                referrer_host="t.me",
                device="mobile",
                browser="Chrome",
                os="Android",
            ),
            _view(
                "a",
                "2026-03-02",
                "/dasturlar/:id",
                hour=13,
                session_id="s1",
                device="mobile",
                browser="Chrome",
                os="Android",
            ),
            _view(
                "b",
                "2026-03-10",
                "/",
                session_id="s2",
                is_entry=True,
                device="desktop",
                browser="Safari",
                os="macOS",
            ),
            # Historic row without a session: counted as a view, not a session.
            _view("c", "2026-03-11", "/login"),
            # April is outside the period.
            _view("d", "2026-04-01"),
        ]
    )
    await session.flush()

    body = await _get(client, "traffic", headers, **MARCH)
    assert _figure(body, "views")["value"] == 4
    assert _figure(body, "visitors")["value"] == 3
    assert _figure(body, "sessions")["value"] == 2
    assert _figure(body, "sessions")["since"] == "2026-03-02"
    assert _figure(body, "new_visitors")["value"] == 2
    assert _figure(body, "returning_visitors")["value"] == 1
    assert body["ratios"][0]["key"] == "views_per_session"
    assert body["ratios"][0]["value"] == 2.0

    pages = {row["path"]: row for row in body["pages"]}
    assert pages["/dasturlar"]["views"] == 1 and pages["/dasturlar"]["visitors"] == 1
    assert {row["path"] for row in body["landing"]} == {"/dasturlar", "/"}
    # Session s1 ended on the course page, s2 on the home page.
    assert {row["path"] for row in body["exits"]} == {"/dasturlar/:id", "/"}
    assert body["referrers"] == [{"key": "t.me", "value": 1, "suppressed": False}]
    assert {g["key"]: g["value"] for g in body["devices"]} == {"mobile": 1, "desktop": 1}
    assert body["collected"]["referrers"] == "2026-03-02"

    series = {s["key"]: s for s in body["series"]}
    assert sum(p["value"] for p in series["views"]["points"]) == 4
    assert sum(p["value"] for p in series["new_visitors"]["points"]) == 2
    assert body["scope"]["period"]["bucket"] == "day"


async def test_staff_browsing_and_management_screens_are_left_out_unless_asked(client, session):
    headers = await _staff(session)
    session.add_all(
        [
            _view("v", "2026-03-02", "/"),
            _view("admin", "2026-03-02", "/admin", is_staff=True),
            _view("old-admin", "2026-03-03", "/admin"),  # pre-0029 row: flagged by path
            _view("bot", "2026-03-03", "/", device="bot"),
        ]
    )
    await session.flush()
    body = await _get(client, "traffic", headers, **MARCH)
    assert _figure(body, "views")["value"] == 1
    with_staff = await _get(client, "traffic", headers, include_staff="true", **MARCH)
    assert _figure(with_staff, "views")["value"] == 3
    assert with_staff["scope"]["includes_staff"] is True


async def test_columns_that_never_recorded_say_so_instead_of_zero(client, session):
    headers = await _staff(session)
    session.add(_view("v", "2026-03-02", "/"))
    await session.flush()
    body = await _get(client, "traffic", headers, **MARCH)
    assert _figure(body, "sessions")["value"] is None
    assert _figure(body, "sessions")["since"] is None
    assert body["collected"] == {"sessions": None, "referrers": None, "devices": None}
    assert body["ratios"][0]["value"] is None


# ------------------------------------------------------------------ accounts


async def test_accounts_count_participants_not_staff_and_follow_onboarding(client, session):
    headers = await _staff(session)
    await _person(session, created=at("2026-03-03"), onboarded=at("2026-03-03", 13))
    await _person(session, created=at("2026-03-04"))
    await _person(session, created=at("2026-02-01"), onboarded=at("2026-03-05"))
    await _person(session, created=at("2026-03-06"), status=UserStatus.DELETED)
    await _person(session, created=at("2026-03-07"), roles=(Role.MODERATOR,))

    body = await _get(client, "accounts", headers, **MARCH)
    assert _figure(body, "registrations")["value"] == 3  # the deleted one registered too
    assert _figure(body, "accounts_total")["value"] == 3
    assert _figure(body, "accounts_deleted")["value"] == 1
    assert _figure(body, "staff_accounts")["value"] == 2  # the moderator and the admin
    assert _figure(body, "onboarded_new")["value"] == 2
    rate = next(r for r in body["rates"] if r["key"] == "onboarding_rate")
    assert (rate["numerator"], rate["denominator"]) == (1, 3)
    assert sum(p["value"] for p in body["series"][0]["points"]) == 3
    started, registered = body["registration"]
    assert started["value"] is None and started["since"] is None
    assert registered["value"] == 3 and registered["of_previous"] is None


async def test_a_stage_recorded_only_from_inside_the_period_is_not_a_denominator(client, session):
    """Fourteen completions over a month against one start recorded today is
    not a 1,400% conversion. The partial stage keeps its count and its date;
    the chain skips it."""
    headers = await _staff(session)
    for _ in range(3):
        await _person(session, created=at("2026-03-02"))
    session.add(
        AnalyticsEvent(
            name="registration_started",
            occurred_at=at("2026-03-20"),
            visitor_hash="late",
            source="client",
        )
    )
    await session.flush()
    body = await _get(client, "accounts", headers, **MARCH)
    started, registered = body["registration"]
    assert started["value"] == 1 and started["since"] == "2026-03-20"
    assert registered["of_previous"] is None

    # Recorded since before the period: a real denominator.
    session.add(
        AnalyticsEvent(
            name="registration_started",
            occurred_at=at("2026-02-01"),
            visitor_hash="early",
            source="client",
        )
    )
    await session.flush()
    insights.clear_cache()
    body = await _get(client, "accounts", headers, **MARCH)
    started, registered = body["registration"]
    assert started["value"] == 1 and started["since"] == "2026-02-01"
    assert registered["of_previous"] == 300.0


async def test_registration_starts_come_from_events_once_they_exist(client, session):
    headers = await _staff(session)
    await _person(session, created=at("2026-03-03"))
    session.add_all(
        [
            AnalyticsEvent(
                name="registration_started",
                occurred_at=at("2026-02-20"),
                visitor_hash="z",
                source="client",
            ),
            AnalyticsEvent(
                name="registration_started",
                occurred_at=at("2026-03-02"),
                visitor_hash="a",
                source="client",
            ),
            AnalyticsEvent(
                name="registration_started",
                occurred_at=at("2026-03-02", 13),
                visitor_hash="a",
                source="client",
            ),
            AnalyticsEvent(
                name="registration_started",
                occurred_at=at("2026-03-03"),
                visitor_hash="b",
                source="client",
            ),
        ]
    )
    await session.flush()
    body = await _get(client, "accounts", headers, **MARCH)
    started, registered = body["registration"]
    assert started["value"] == 2 and started["since"] == "2026-02-20"
    assert registered["of_previous"] == 50.0


# ---------------------------------------------------------------- engagement


async def test_active_users_mean_a_recorded_action_not_a_page_view(client, session):
    headers = await _staff(session)
    now = datetime.now(UTC)
    today = (now + timedelta(hours=5)).date().isoformat()
    day_start = at(today, 0)

    def earlier_today(hours: int) -> datetime:
        # `hours` ago, but never before today's Tashkent midnight: run just
        # after midnight, the plain subtraction lands on yesterday, and the
        # period figures below (which filter by local calendar day) miss it.
        return max(now - timedelta(hours=hours), min(now, day_start + timedelta(minutes=1)))

    program = await _program(session)
    doer = await _person(session, created=now - timedelta(days=40))
    reader = await _person(session, created=now - timedelta(days=40))
    newcomer = await _person(session, created=earlier_today(2))
    session.add_all(
        [
            Enrollment(
                user_id=doer,
                program_id=program.id,
                started_at=earlier_today(3),
                last_activity_at=earlier_today(3),
            ),
            PageView(
                visitor_hash="r",
                path="/kabinet",
                user_id=reader,
                is_authenticated=True,
                occurred_at=earlier_today(1),
            ),
            PageView(
                visitor_hash="n",
                path="/kabinet",
                user_id=newcomer,
                is_authenticated=True,
                occurred_at=earlier_today(1),
            ),
        ]
    )
    await session.flush()
    body = await _get(client, "engagement", headers, date_from=today, date_to=today)
    active = {f["key"]: f["value"] for f in body["active"]}
    assert active["engaged_1d"] == 1
    assert active["visited_1d"] == 2
    assert active["engaged_users"] == 1 and active["visited_users"] == 2
    # Of the two who did or saw something today, only the enroller registered before today.
    assert body["returning"]["value"] == 1
    assert body["visits_since"] == today
    cohorts = body["cohorts"]
    assert sum(c["size"] for c in cohorts) == 3
    newest = max(cohorts, key=lambda c: c["starts"])
    assert newest["cells"][0]["complete"] is False


# -------------------------------------------------------------------- funnel


async def test_the_journey_funnel_follows_the_accounts_opened_in_the_period(client, session):
    headers = await _staff(session)
    program = await _program(session)
    full = await _person(session, created=at("2026-03-01"), onboarded=at("2026-03-01", 13))
    half = await _person(session, created=at("2026-03-02"), onboarded=at("2026-03-02", 13))
    await _person(session, created=at("2026-03-03"))
    earlier = await _person(session, created=at("2026-01-03"), onboarded=at("2026-01-03", 13))

    assessment = Assessment(user_id=full, completed_at=at("2026-03-01", 14), overall_score=55)
    session.add(assessment)
    await session.flush()
    session.add_all(
        [
            DevelopmentScore(
                user_id=full,
                assessment_id=assessment.id,
                dimension=ScoreDimension.EDUCATION_SKILLS,
                current=55,
            ),
            Enrollment(
                user_id=full,
                program_id=program.id,
                status=EnrollmentStatus.COMPLETED,
                progress_percent=100,
                started_at=at("2026-03-02"),
                completed_at=at("2026-03-20"),
            ),
            Enrollment(
                user_id=half,
                program_id=program.id,
                status=EnrollmentStatus.ENROLLED,
                progress_percent=0,
                started_at=at("2026-03-05"),
            ),
            Enrollment(
                user_id=earlier,
                program_id=program.id,
                status=EnrollmentStatus.COMPLETED,
                progress_percent=100,
                started_at=at("2026-01-05"),
                completed_at=at("2026-03-10"),
            ),
            PageView(visitor_hash="x", path="/", occurred_at=at("2026-03-04")),
            PageView(visitor_hash="y", path="/", occurred_at=at("2026-03-04")),
        ]
    )
    await session.flush()

    body = await _get(client, "funnel", headers, **MARCH)
    journey = {s["key"]: s for s in body["journey"]}
    assert journey["registered"]["value"] == 3
    assert journey["onboarded"]["value"] == 2 and journey["onboarded"]["of_previous"] == 66.7
    assert journey["test_started"]["value"] is None  # never recorded
    assert journey["test_completed"]["value"] == 1
    # The chain continues from the last known stage, not from the unknown one.
    assert journey["test_completed"]["of_previous"] == 50.0
    assert journey["score_received"]["value"] == 1
    assert journey["program_enrolled"]["value"] == 2
    assert journey["program_started"]["value"] == 1
    assert journey["program_completed"]["value"] == 1
    assert journey["program_completed"]["of_first"] == 33.3

    acquisition = {s["key"]: s for s in body["acquisition"]}
    assert acquisition["visitors"]["value"] == 2
    assert acquisition["registered"]["value"] == 3

    diagnostic = {s["key"]: s for s in body["diagnostic"]}
    assert diagnostic["test_started"]["value"] is None
    assert diagnostic["test_completed"]["value"] == 1


# ------------------------------------------------------------------ programs


async def test_programs_rank_by_real_enrollments_and_read_views_from_events(client, session):
    headers = await _staff(session)
    python = await _program(session, "python")
    design = await _program(session, "design")
    a = await _person(session, created=at("2026-02-01"))
    b = await _person(session, created=at("2026-02-01"))
    c = await _person(session, created=at("2026-02-01"))
    session.add_all(
        [
            Enrollment(
                user_id=a,
                program_id=python.id,
                status=EnrollmentStatus.COMPLETED,
                progress_percent=100,
                started_at=at("2026-03-01"),
                completed_at=at("2026-03-15"),
                last_activity_at=at("2026-03-15"),
            ),
            Enrollment(
                user_id=b,
                program_id=python.id,
                status=EnrollmentStatus.IN_PROGRESS,
                progress_percent=20,
                started_at=at("2026-03-02"),
                last_activity_at=at("2026-03-02"),
            ),
            Enrollment(
                user_id=c,
                program_id=python.id,
                status=EnrollmentStatus.ENROLLED,
                progress_percent=0,
                started_at=at("2026-03-03"),
                last_activity_at=at("2026-03-03"),
            ),
            Enrollment(
                user_id=a,
                program_id=design.id,
                status=EnrollmentStatus.ENROLLED,
                progress_percent=0,
                started_at=at("2026-03-04"),
            ),
            AnalyticsEvent(
                name="program_viewed",
                occurred_at=at("2026-03-01"),
                visitor_hash="v1",
                source="client",
                entity_type="program",
                entity_id=str(python.id),
            ),
            AnalyticsEvent(
                name="program_viewed",
                occurred_at=at("2026-03-01", 13),
                visitor_hash="v1",
                source="client",
                entity_type="program",
                entity_id=str(python.id),
            ),
            AnalyticsEvent(
                name="program_viewed",
                occurred_at=at("2026-03-02"),
                visitor_hash="v2",
                source="client",
                entity_type="program",
                entity_id=str(python.id),
            ),
        ]
    )
    await session.flush()

    body = await _get(client, "programs", headers, **MARCH)
    assert body["total"] == 2
    first, second = body["items"]
    assert first["slug"] == "python"
    assert (first["enrolled"], first["started"], first["completed"]) == (3, 2, 1)
    assert first["in_progress_now"] == 2
    assert first["stalled"] == 2  # two March enrollments, untouched for weeks, not done
    assert first["completion"] == {
        "key": "completion",
        "numerator": 1,
        "denominator": 3,
        "value": 33.3,
    }
    assert (first["views"], first["viewers"]) == (3, 2)
    assert second["slug"] == "design" and second["views"] == 0
    assert body["views_since"] == "2026-03-01"

    by_title = await _get(
        client, "programs", headers, sort="title", order="asc", lang="en", **MARCH
    )
    assert [row["slug"] for row in by_title["items"]] == ["design", "python"]
    searched = await _get(client, "programs", headers, search="Курс des", **MARCH)
    assert [row["slug"] for row in searched["items"]] == ["design"]


# ------------------------------------------------------------------ activity


async def test_the_timeline_shows_events_without_identifying_anyone(client, session):
    headers = await _staff(session)
    program = await _program(session)
    user_id = await _person(session, created=at("2026-03-01"), region=Region.BUKHARA)
    session.add_all(
        [
            AnalyticsEvent(
                name="registration_completed",
                occurred_at=at("2026-03-01"),
                user_id=user_id,
                properties={"provider": "password"},
            ),
            AnalyticsEvent(
                name="program_enrolled",
                occurred_at=at("2026-03-02"),
                user_id=user_id,
                entity_type="program",
                entity_id=str(program.id),
            ),
            AnalyticsEvent(name="login_completed", occurred_at=at("2026-03-03"), user_id=user_id),
            AnalyticsEvent(
                name="program_viewed",
                occurred_at=at("2026-03-03"),
                visitor_hash="v",
                source="client",
            ),
        ]
    )
    await session.flush()
    body = await _get(client, "activity", headers, **MARCH)
    assert body["total"] == 2  # logins and browser events are not a story
    enrolled, registered = body["items"]
    assert enrolled["name"] == "program_enrolled"
    assert enrolled["title_i18n"]["uz"] == "Kurs python"
    assert enrolled["region"] == "bukhara"
    assert enrolled["actor"] == registered["actor"] and len(enrolled["actor"]) == 6
    assert str(user_id) not in str(body)
    assert registered["properties"] == {}  # the provider is not for this screen
    assert body["since"] == "2026-03-01"
    page_two = await _get(client, "activity", headers, page=2, size=1, **MARCH)
    assert [item["name"] for item in page_two["items"]] == ["registration_completed"]


# --------------------------------------------------------------------- live


async def test_online_now_reads_presence_within_the_window(client, session):
    headers = await _staff(session)
    now = datetime.now(UTC)
    user_id = await _person(session, created=at("2026-01-01"))
    session.add_all(
        [
            VisitorPresence(
                visitor_hash="a", last_seen_at=now - timedelta(minutes=1), user_id=user_id
            ),
            VisitorPresence(visitor_hash="b", last_seen_at=now - timedelta(minutes=4)),
            VisitorPresence(visitor_hash="c", last_seen_at=now - timedelta(minutes=9)),
            VisitorPresence(visitor_hash="s", last_seen_at=now, is_staff=True),
        ]
    )
    await session.flush()
    body = await _get(client, "live", headers)
    assert body["online_now"] == 2 and body["online_signed_in"] == 1
    assert body["window_minutes"] == 5


# ------------------------------------------------------------------- errors


async def test_errors_are_grouped_by_route_and_status(client, session):
    headers = await _staff(session)
    session.add_all(
        [
            AnalyticsEvent(
                name="api_error",
                occurred_at=at("2026-03-02"),
                properties={"route": "/api/v1/plans/generate", "status": 500},
            ),
            AnalyticsEvent(
                name="api_error",
                occurred_at=at("2026-03-03"),
                properties={"route": "/api/v1/plans/generate", "status": 500},
            ),
            AnalyticsEvent(
                name="api_error",
                occurred_at=at("2026-03-03"),
                properties={"route": "/api/v1/auth/otp/request", "status": 503},
            ),
            AnalyticsEvent(
                name="client_error",
                occurred_at=at("2026-03-03"),
                source="client",
                path="/kabinet",
                properties={"name": "TypeError"},
            ),
        ]
    )
    await session.flush()
    body = await _get(client, "errors", headers, **MARCH)
    assert _figure(body, "api_errors")["value"] == 3
    assert _figure(body, "client_errors")["value"] == 1
    assert body["by_route"][0] == {"key": "/api/v1/plans/generate", "value": 2, "suppressed": False}
    assert {g["key"]: g["value"] for g in body["by_status"]} == {"500": 2, "503": 1}
    assert body["since"] == "2026-03-02"


async def test_errors_never_recorded_are_not_zero(client, session):
    headers = await _staff(session)
    body = await _get(client, "errors", headers, **MARCH)
    assert _figure(body, "api_errors")["value"] is None
    assert body["since"] is None


# -------------------------------------------------------------------- cache


async def test_answers_are_cached_briefly_and_say_so(client, session):
    headers = await _staff(session)
    first = await _get(client, "traffic", headers, **MARCH)
    assert first["scope"]["cached"] is False
    second = await _get(client, "traffic", headers, **MARCH)
    assert second["scope"]["cached"] is True
    assert second["scope"]["cache_ttl_seconds"] == insights.CACHE_TTL


async def test_definitions_cover_every_figure_served(client, session):
    headers = await _staff(session)
    definitions = await _get(client, "definitions", headers)
    program = await _program(session)
    user_id = await _person(session, created=at("2026-03-01"))
    session.add(Enrollment(user_id=user_id, program_id=program.id, started_at=at("2026-03-02")))
    await session.flush()
    keys: set[str] = set()
    for endpoint in (
        "overview",
        "traffic",
        "accounts",
        "engagement",
        "funnel",
        "features",
        "errors",
    ):
        body = await _get(client, endpoint, headers, **MARCH)
        for field in (
            "figures",
            "today",
            "period",
            "active",
            "rates",
            "ratios",
            "acquisition",
            "journey",
            "diagnostic",
            "registration",
        ):
            for item in body.get(field, []):
                keys.add(item["key"])
        if "returning" in body:
            keys.add(body["returning"]["key"])
    missing = {k for k in keys if k not in definitions}
    assert not missing, missing
