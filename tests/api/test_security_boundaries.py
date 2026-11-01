"""The security boundaries, exercised the way an attacker would.

Each test is one claim the platform makes, tried from the wrong side: another
woman's record, an administrator's door, a forged or stale or revoked token,
a partner writing to accounts it does not know, a reviewer outside her
relationship, a model asked for without end. Every one expects a refusal,
and several check that the refusal left no trace a legitimate call would not.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.constants import (
    ApplicationStatus,
    GoalHorizon,
    Language,
    OpportunitySource,
    OpportunityType,
    ProgramCategory,
    ProgramFormat,
    Region,
    Role,
    TaskStatus,
    UserStatus,
)
from app.core.security import create_token
from app.models.audit import AuditLog, RiskFlag
from app.models.auth_token import RevokedToken
from app.models.mentor import MentorProfile, MentorSession
from app.models.opportunity import Application, Opportunity, OutcomeRecord
from app.models.practice import PracticalTask, TaskAttempt
from app.models.profile import Goal, Profile
from app.models.program import Program
from app.models.user import User, UserRole

API = "/api/v1"


async def _account(
    session,
    *,
    roles: tuple[Role, ...] = (Role.USER,),
    region: Region | None = Region.TASHKENT_CITY,
    status: UserStatus = UserStatus.ACTIVE,
    scope_region: Region | None = None,
    profile: bool = True,
) -> User:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"{user_id.hex[:10]}@example.uz",
        language=Language.UZ,
        region=region,
        status=status,
    )
    session.add(user)
    await session.flush()
    for role in roles:
        session.add(
            UserRole(
                user_id=user_id,
                role=role,
                scope_region=scope_region if role == Role.REGIONAL_COORDINATOR else None,
            )
        )
    if profile:
        session.add(Profile(user_id=user_id, full_name=f"Woman {user_id.hex[:4]}"))
    await session.flush()
    return user


def _token(user: User, *roles: Role, token_type: str = "access") -> str:
    primary = roles[0] if roles else Role.USER
    return create_token(user.id, primary, token_type, {"roles": [r.value for r in roles]})


def _headers(user: User, *roles: Role) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(user, *(roles or (Role.USER,)))}"}


# ---------------------------------------------------------------------------
# 1. User isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_participant_never_reads_or_edits_another_womans_records(client, session):
    alice = await _account(session)
    bob = await _account(session)
    goal = Goal(user_id=bob.id, title="Bob's goal", horizon=GoalHorizon.M6)
    session.add(goal)
    await session.flush()

    # Another profile by id: the staff route refuses a participant outright.
    assert (
        await client.get(f"{API}/users/{bob.id}/profile", headers=_headers(alice))
    ).status_code == 403
    # Another woman's goal: not hers, so it does not exist for her.
    assert (
        await client.patch(
            f"{API}/users/me/goals/{goal.id}", json={"title": "x"}, headers=_headers(alice)
        )
    ).status_code == 404
    assert (
        await client.delete(f"{API}/users/me/goals/{goal.id}", headers=_headers(alice))
    ).status_code == 404
    assert (await session.get(Goal, goal.id)).title == "Bob's goal"

    # Her own record is hers.
    mine = await client.get(f"{API}/users/me/profile", headers=_headers(alice))
    assert mine.status_code == 200
    assert mine.json()["full_name"] == f"Woman {alice.id.hex[:4]}"


# ---------------------------------------------------------------------------
# 2. Admin protection and 3. role manipulation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_endpoints_refuse_a_participant(client, session):
    alice = await _account(session)
    for path in (
        "/admin/users",
        "/admin/audit",
        "/admin/users/roster",
        "/admin/users/roster/export",
        "/admin/analytics/overview",
        "/admin/results/overview",
        "/admin/traffic",
        "/ai/risk-flags",
        "/integrations/outbox/failed",
    ):
        response = await client.get(f"{API}{path}", headers=_headers(alice))
        assert response.status_code == 403, path
    response = await client.post(
        f"{API}/admin/users/{alice.id}/roles", json={"role": "admin"}, headers=_headers(alice)
    )
    assert response.status_code == 403
    # Without any token the answer is 401, never a page.
    assert (await client.get(f"{API}/admin/users")).status_code == 401


@pytest.mark.asyncio
async def test_a_token_that_merely_claims_admin_is_worth_nothing(client, session):
    """The signing key is the only thing a forged claim lacks — but a *validly
    signed* token whose role was revoked, or never granted, must also fail.
    Roles are re-read from the database on every request."""
    alice = await _account(session)
    claimed = _headers(alice, Role.ADMIN)
    assert (await client.get(f"{API}/admin/users", headers=claimed)).status_code == 403

    session.add(UserRole(user_id=alice.id, role=Role.ADMIN))
    await session.flush()
    assert (await client.get(f"{API}/admin/users", headers=claimed)).status_code == 200

    # Revoked in the database: the same unexpired token stops working at once.
    assignment = await session.scalar(
        select(UserRole).where(UserRole.user_id == alice.id, UserRole.role == Role.ADMIN)
    )
    await session.delete(assignment)
    await session.flush()
    assert (await client.get(f"{API}/admin/users", headers=claimed)).status_code == 403


@pytest.mark.asyncio
async def test_roles_and_status_cannot_be_changed_through_profile_endpoints(client, session):
    alice = await _account(session)
    response = await client.patch(
        f"{API}/users/me",
        json={"role": "admin", "roles": ["admin"], "status": "active", "language": "ru"},
        headers=_headers(alice),
    )
    assert response.status_code == 200
    assert response.json()["language"] == "ru"
    roles = set(
        (await session.scalars(select(UserRole.role).where(UserRole.user_id == alice.id))).all()
    )
    assert roles == {Role.USER}
    assert (await client.get(f"{API}/admin/users", headers=_headers(alice))).status_code == 403

    # The age band feeds the health gate and is derived from the birth date;
    # a band sent by the client is ignored.
    response = await client.put(
        f"{API}/users/me/profile",
        json={"birth_date": None, "age_group": "18+", "bio": "hello"},
        headers=_headers(alice),
    )
    assert response.status_code == 200
    profile = await session.scalar(select(Profile).where(Profile.user_id == alice.id))
    assert profile.age_group is None
    assert profile.bio == "hello"


# ---------------------------------------------------------------------------
# 4. Sessions: suspension, sign-out, refresh rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_suspended_or_deleted_account_is_refused_at_once(client, session):
    suspended = await _account(session, status=UserStatus.SUSPENDED)
    deleted = await _account(session, status=UserStatus.DELETED)
    for user in (suspended, deleted):
        response = await client.get(f"{API}/users/me", headers=_headers(user))
        assert response.status_code == 401
        assert response.json()["detail"] == "Account is not active"


async def _register(client, email: str) -> dict:
    response = await client.post(
        f"{API}/auth/register",
        json={"email": email, "password": "long-enough-password", "accepted_privacy_policy": True},
        headers={"X-Refresh-Transport": "body"},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_signing_out_revokes_both_tokens(client, session):
    pair = await _register(client, "signout@example.uz")
    headers = {"Authorization": f"Bearer {pair['access_token']}"}
    assert (await client.get(f"{API}/users/me", headers=headers)).status_code == 200

    response = await client.post(
        f"{API}/auth/logout", json={"refresh_token": pair["refresh_token"]}, headers=headers
    )
    assert response.status_code == 200

    # The access token that signed the call is dead...
    refused = await client.get(f"{API}/users/me", headers=headers)
    assert refused.status_code == 401
    assert refused.json()["detail"] == "Token revoked"
    # ...and so is the refresh token: the session cannot be resumed.
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    ).status_code == 401
    reasons = set((await session.scalars(select(RevokedToken.reason))).all())
    assert reasons == {"signed_out"}


@pytest.mark.asyncio
async def test_a_refresh_token_is_spent_on_use(client, session):
    pair = await _register(client, "rotate@example.uz")
    first = await client.post(f"{API}/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert first.status_code == 200
    fresh = first.json()
    assert fresh["refresh_token"] != pair["refresh_token"]

    # The spent token is refused — and its reuse is treated as theft: every
    # session of the account ends, the fresh token included.
    replay = await client.post(f"{API}/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert replay.status_code == 401
    again = await client.post(f"{API}/auth/refresh", json={"refresh_token": fresh["refresh_token"]})
    assert again.status_code == 401
    assert (
        await client.get(
            f"{API}/users/me", headers={"Authorization": f"Bearer {fresh['access_token']}"}
        )
    ).status_code == 401
    # A refresh token is never accepted where an access token is expected.
    assert (
        await client.get(
            f"{API}/users/me", headers={"Authorization": f"Bearer {fresh['refresh_token']}"}
        )
    ).status_code == 401


@pytest.mark.asyncio
async def test_a_tampered_or_unsigned_token_is_refused(client, session):
    alice = await _account(session)
    good = _token(alice, Role.USER)
    header, payload, signature = good.split(".")
    # Same claims, signature of another token: the signature does not match.
    other = _token(alice, Role.ADMIN).split(".")[2]
    for forged in (f"{header}.{payload}.{other}", f"{header}.{payload}.", "not-a-token"):
        response = await client.get(
            f"{API}/users/me", headers={"Authorization": f"Bearer {forged}"}
        )
        assert response.status_code == 401
    # `alg: none` is not an option the server knows.
    none_header = (
        __import__("base64")
        .urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        .rstrip(b"=")
        .decode()
    )
    response = await client.get(
        f"{API}/users/me", headers={"Authorization": f"Bearer {none_header}.{payload}."}
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 5. Regional scope cannot be widened
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_coordinator_sees_her_region_only_and_cannot_widen_it(client, session):
    scoped = await _account(
        session,
        roles=(Role.REGIONAL_COORDINATOR,),
        region=Region.TASHKENT_CITY,
        scope_region=Region.TASHKENT_CITY,
    )
    unscoped = await _account(session, roles=(Role.REGIONAL_COORDINATOR,), region=Region.SAMARKAND)
    await _account(session, region=Region.TASHKENT_CITY)
    await _account(session, region=Region.SAMARKAND)

    listed = await client.get(
        f"{API}/admin/users?region=samarkand", headers=_headers(scoped, Role.REGIONAL_COORDINATOR)
    )
    assert listed.status_code == 200
    assert {row["region"] for row in listed.json()} == {"tashkent_city"}

    # Changing her own account's region does not move her scope.
    response = await client.patch(
        f"{API}/users/me",
        json={"region": "samarkand"},
        headers=_headers(scoped, Role.REGIONAL_COORDINATOR),
    )
    assert response.status_code == 200
    listed = await client.get(
        f"{API}/admin/users", headers=_headers(scoped, Role.REGIONAL_COORDINATOR)
    )
    assert {row["region"] for row in listed.json()} == {"tashkent_city"}

    # A coordinator nobody scoped is refused, not shown the country — even
    # with no region on her account at all.
    for path in ("/admin/users", "/admin/dashboard", "/admin/kpi", "/ai/risk-flags"):
        response = await client.get(
            f"{API}{path}", headers=_headers(unscoped, Role.REGIONAL_COORDINATOR)
        )
        assert response.status_code == 403, path
        assert response.json()["detail"] == {"reason": "no_region_scope"}
    await client.patch(
        f"{API}/users/me",
        json={"region": None},
        headers=_headers(unscoped, Role.REGIONAL_COORDINATOR),
    )
    assert (
        await client.get(
            f"{API}/admin/users", headers=_headers(unscoped, Role.REGIONAL_COORDINATOR)
        )
    ).status_code == 403
    # National figures are for national staff.
    assert (
        await client.get(
            f"{API}/admin/traffic", headers=_headers(scoped, Role.REGIONAL_COORDINATOR)
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_risk_flags_are_scoped_to_the_coordinators_region(client, session):
    coordinator = await _account(
        session,
        roles=(Role.REGIONAL_COORDINATOR,),
        region=Region.TASHKENT_CITY,
        scope_region=Region.TASHKENT_CITY,
    )
    near = await _account(session, region=Region.TASHKENT_CITY)
    far = await _account(session, region=Region.SAMARKAND)
    for user in (near, far):
        session.add(
            RiskFlag(
                user_id=user.id,
                flag_type="inactivity",
                severity="medium",
                reason="quiet",
                evidence={},
            )
        )
    await session.flush()
    flags = await client.get(
        f"{API}/ai/risk-flags", headers=_headers(coordinator, Role.REGIONAL_COORDINATOR)
    )
    assert flags.status_code == 200
    assert {flag["user_id"] for flag in flags.json()} == {str(near.id)}
    far_flag = await session.scalar(select(RiskFlag.id).where(RiskFlag.user_id == far.id))
    assert (
        await client.post(
            f"{API}/ai/risk-flags/{far_flag}/resolve",
            headers=_headers(coordinator, Role.REGIONAL_COORDINATOR),
        )
    ).status_code == 404


# ---------------------------------------------------------------------------
# 6. Staff reads of personal data are by relationship, and audited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mentor_reads_only_the_women_she_mentors_and_every_read_is_logged(client, session):
    mentor = await _account(session, roles=(Role.USER, Role.MENTOR))
    stranger = await _account(session)
    mentee = await _account(session)
    profile = MentorProfile(user_id=mentor.id, expertise=["sales"], languages=["uz"])
    session.add(profile)
    await session.flush()
    session.add(
        MentorSession(
            mentor_id=profile.id,
            user_id=mentee.id,
            topic="career",
            scheduled_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await session.flush()

    headers = _headers(mentor, Role.USER, Role.MENTOR)
    assert (
        await client.get(f"{API}/users/{stranger.id}/profile", headers=headers)
    ).status_code == 404
    seen = await client.get(f"{API}/users/{mentee.id}/profile", headers=headers)
    assert seen.status_code == 200
    assert "marital_status" not in seen.json()

    logged = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "user.profile_view", AuditLog.entity_id == str(mentee.id))
    )
    assert logged == 1


@pytest.mark.asyncio
async def test_a_coordinator_reads_profiles_in_her_region_only(client, session):
    coordinator = await _account(
        session,
        roles=(Role.REGIONAL_COORDINATOR,),
        region=Region.TASHKENT_CITY,
        scope_region=Region.TASHKENT_CITY,
    )
    near = await _account(session, region=Region.TASHKENT_CITY)
    far = await _account(session, region=Region.SAMARKAND)
    headers = _headers(coordinator, Role.REGIONAL_COORDINATOR)
    assert (await client.get(f"{API}/users/{near.id}/profile", headers=headers)).status_code == 200
    assert (await client.get(f"{API}/users/{far.id}/profile", headers=headers)).status_code == 404


# ---------------------------------------------------------------------------
# 7. Partner callbacks
# ---------------------------------------------------------------------------


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _listing(session, source: OpportunitySource) -> Opportunity:
    listing = Opportunity(
        source=source,
        external_id=uuid.uuid4().hex,
        type=OpportunityType.VACANCY,
        title_i18n={"uz": "Ish"},
        required_skills=[],
        eligibility={},
        reward={},
        is_active=True,
    )
    session.add(listing)
    await session.flush()
    return listing


@pytest.mark.asyncio
async def test_a_partner_decides_only_its_own_applications(client, session, monkeypatch):
    monkeypatch.setattr(settings, "edu_job_client_id", "edu-client")
    monkeypatch.setattr(settings, "edu_job_client_secret", None)
    woman = await _account(session)
    ours = await _listing(session, OpportunitySource.ORGANIZATION)
    theirs = await _listing(session, OpportunitySource.EDU_JOB)
    for listing in (ours, theirs):
        session.add(
            Application(
                user_id=woman.id,
                opportunity_id=listing.id,
                status=ApplicationStatus.SUBMITTED,
                submitted_at=datetime.now(UTC),
            )
        )
    await session.flush()
    own = await session.scalar(select(Application).where(Application.opportunity_id == ours.id))
    partner = await session.scalar(
        select(Application).where(Application.opportunity_id == theirs.id)
    )

    body = {"status": "accepted"}
    # No credential: refused. Wrong listing: not theirs. Theirs: decided.
    assert (
        await client.post(f"{API}/integrations/edu_job/applications/{partner.id}/status", json=body)
    ).status_code == 401
    assert (
        await client.post(
            f"{API}/integrations/edu_job/applications/{own.id}/status",
            json=body,
            headers={"X-Client-Id": "edu-client"},
        )
    ).status_code == 404
    assert (await session.get(Application, own.id)).status == ApplicationStatus.SUBMITTED
    assert (
        await client.post(
            f"{API}/integrations/edu_job/applications/{partner.id}/status",
            json=body,
            headers={"X-Client-Id": "edu-client"},
        )
    ).status_code == 200
    # A decided application is final.
    assert (
        await client.post(
            f"{API}/integrations/edu_job/applications/{partner.id}/status",
            json={"status": "rejected"},
            headers={"X-Client-Id": "edu-client"},
        )
    ).status_code == 409


@pytest.mark.asyncio
async def test_a_partner_reports_outcomes_only_for_women_it_knows_and_signs_them(
    client, session, monkeypatch
):
    monkeypatch.setattr(settings, "edu_job_client_id", "edu-client")
    monkeypatch.setattr(settings, "edu_job_client_secret", "edu-secret")
    stranger = await _account(session)
    applicant = await _account(session)
    listing = await _listing(session, OpportunitySource.EDU_JOB)
    application = Application(
        user_id=applicant.id,
        opportunity_id=listing.id,
        status=ApplicationStatus.SUBMITTED,
        submitted_at=datetime.now(UTC),
    )
    session.add(application)
    await session.flush()

    payload = {
        "source": "edu_job",
        "outcome_type": "employment",
        "application_id": str(application.id),
    }
    raw = json.dumps(payload).encode()
    headers = {"X-Client-Id": "edu-client", "Content-Type": "application/json"}

    # A secret is configured, so an unsigned or mis-signed call is refused.
    assert (
        await client.post(
            f"{API}/integrations/edu_job/outcomes?user_id={applicant.id}",
            content=raw,
            headers=headers,
        )
    ).status_code == 401
    assert (
        await client.post(
            f"{API}/integrations/edu_job/outcomes?user_id={applicant.id}",
            content=raw,
            headers={**headers, "X-Signature": _sign("wrong", raw)},
        )
    ).status_code == 401
    signed = {**headers, "X-Signature": _sign("edu-secret", raw)}
    # The application belongs to somebody else: no outcome for the stranger.
    assert (
        await client.post(
            f"{API}/integrations/edu_job/outcomes?user_id={stranger.id}",
            content=raw,
            headers=signed,
        )
    ).status_code == 404
    # No application and no consent: the partner does not know her.
    bare = json.dumps({"source": "edu_job", "outcome_type": "employment"}).encode()
    assert (
        await client.post(
            f"{API}/integrations/edu_job/outcomes?user_id={stranger.id}",
            content=bare,
            headers={**headers, "X-Signature": _sign("edu-secret", bare)},
        )
    ).status_code == 403
    # Her own application, properly signed: recorded.
    assert (
        await client.post(
            f"{API}/integrations/edu_job/outcomes?user_id={applicant.id}",
            content=raw,
            headers=signed,
        )
    ).status_code == 201
    assert (await session.scalar(select(func.count()).select_from(OutcomeRecord))) == 1


# ---------------------------------------------------------------------------
# 8. Reviewers stay within their relationships; authors own their content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mentor_assesses_only_her_mentees_work(client, session):
    mentor = await _account(session, roles=(Role.USER, Role.MENTOR))
    learner = await _account(session)
    task = PracticalTask(
        slug=f"budget-{uuid.uuid4().hex[:6]}",
        title_i18n={"uz": "Byudjet"},
        is_published=True,
        published_at=datetime.now(UTC),
    )
    session.add(task)
    await session.flush()
    attempt = TaskAttempt(
        user_id=learner.id,
        task_id=task.id,
        status=TaskStatus.SUBMITTED,
        submitted_at=datetime.now(UTC),
        submission={"text": "my budget"},
    )
    session.add(attempt)
    await session.flush()

    headers = _headers(mentor, Role.USER, Role.MENTOR)
    queue = await client.get(f"{API}/practical-tasks/review", headers=headers)
    assert queue.status_code == 200 and queue.json() == []
    verdict = await client.post(
        f"{API}/practical-tasks/attempts/{attempt.id}/evaluate",
        json={"passed": True, "score": 90, "feedback": "fine", "criteria_met": []},
        headers=headers,
    )
    assert verdict.status_code == 404
    assert (await session.get(TaskAttempt, attempt.id)).status == TaskStatus.SUBMITTED


@pytest.mark.asyncio
async def test_content_is_edited_by_its_author_or_a_moderator_only(client, session):
    author = await _account(session, roles=(Role.USER, Role.TRAINER))
    other = await _account(session, roles=(Role.USER, Role.TRAINER))
    moderator = await _account(session, roles=(Role.USER, Role.MODERATOR))
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:6]}",
        title_i18n={"uz": "Kurs"},
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        author_id=author.id,
        is_published=False,
    )
    session.add(program)
    await session.flush()

    body = {"title_i18n": {"uz": "Boshqa nom"}}
    assert (
        await client.patch(
            f"{API}/programs/{program.id}",
            json=body,
            headers=_headers(other, Role.USER, Role.TRAINER),
        )
    ).status_code == 403
    assert (
        await client.patch(
            f"{API}/programs/{program.id}",
            json=body,
            headers=_headers(author, Role.USER, Role.TRAINER),
        )
    ).status_code == 200
    assert (
        await client.patch(
            f"{API}/programs/{program.id}",
            json={"is_published": True},
            headers=_headers(moderator, Role.USER, Role.MODERATOR),
        )
    ).status_code == 200


# ---------------------------------------------------------------------------
# 9. The assistant cannot be made to spend without limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guest_assistant_calls_are_capped_per_address_whatever_the_header(
    client, session, monkeypatch
):
    monkeypatch.setattr(settings, "ai_guest_ip_hourly_limit", 2)
    body = {"message": "WomanUP nima?", "language": "uz"}
    codes = []
    for n in range(3):
        response = await client.post(
            f"{API}/assistant/ask",
            json=body,
            headers={"X-Guest-Id": f"guest-{n}-{uuid.uuid4().hex}"},
        )
        codes.append(response.status_code)
    assert codes[:2] == [200, 200]
    assert codes[2] == 429
    assert response.json()["detail"] == "ai_limit_reached"


@pytest.mark.asyncio
async def test_signed_in_assistant_calls_are_capped_per_account(client, session, monkeypatch):
    monkeypatch.setattr(settings, "ai_user_hourly_limit", 1)
    alice = await _account(session)
    body = {"message": "WomanUP nima?", "language": "uz"}
    assert (
        await client.post(f"{API}/assistant/ask", json=body, headers=_headers(alice))
    ).status_code == 200
    assert (
        await client.post(f"{API}/assistant/ask", json=body, headers=_headers(alice))
    ).status_code == 429
    # The coach shares the account's allowance.
    assert (
        await client.post(f"{API}/assistant/coach", json=body, headers=_headers(alice))
    ).status_code == 429


# ---------------------------------------------------------------------------
# 10. The API's own hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_errors_do_not_echo_what_was_sent(client):
    response = await client.post(
        f"{API}/auth/login", json={"email": "not-an-address", "password": "hunter2-secret"}
    )
    assert response.status_code == 422
    assert "hunter2-secret" not in response.text
    assert "not-an-address" not in response.text
    assert all(set(error) <= {"loc", "msg", "type"} for error in response.json()["errors"])


@pytest.mark.asyncio
async def test_every_answer_carries_the_security_headers_and_bodies_are_bounded(client):
    response = await client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]

    personal = await client.get(f"{API}/programs")
    assert personal.headers["cache-control"] == "no-store"

    huge = await client.post(
        f"{API}/auth/login",
        content=b"x" * (settings.max_request_body_bytes + 1),
        headers={"Content-Type": "application/json"},
    )
    assert huge.status_code == 413
    assert huge.json()["code"] == "body_too_large"


@pytest.mark.asyncio
async def test_the_address_limits_read_the_proxys_entry_not_the_clients(client):
    """`X-Forwarded-For` is honoured for the address the ingress appended —
    the last one — so a client cannot pick its own identity for a limit."""
    from fastapi import Request

    from app.api.deps import client_ip

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"1.1.1.1, 2.2.2.2, 203.0.113.9")],
        "client": ("10.0.0.1", 1234),
        "method": "GET",
        "path": "/",
        "query_string": b"",
    }
    assert client_ip(Request(scope)) == "203.0.113.9"
    scope["headers"] = []
    assert client_ip(Request(scope)) == "10.0.0.1"
