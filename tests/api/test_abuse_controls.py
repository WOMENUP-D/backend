"""Controls against automated abuse and against data outliving its purpose.

A flood from one address is refused before it reaches a handler; two tabs
spending the same refresh token get one new session, not two; a failed
sign-in leaves a line an operator can alert on and nothing an attacker can
use; and what the portal keeps about a woman is bounded in time, and goes
when she asks.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import main as main_module
from app.core.config import settings
from app.core.constants import Language, Role, UserStatus
from app.core.security import create_token
from app.models.audit import AiInteraction
from app.models.auth_token import RevokedToken
from app.models.rate_limit import AuthThrottle
from app.models.user import OtpChallenge, User, UserRole
from app.services import retention

API = "/api/v1"


@pytest.fixture(autouse=True)
def fresh_throttles():
    main_module.general_throttle.reset()
    main_module.expensive_throttle.reset()
    yield
    main_module.general_throttle.reset()
    main_module.expensive_throttle.reset()


async def _user(session) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:8]}@example.uz",
        language=Language.UZ,
        status=UserStatus.ACTIVE,
    )
    session.add(user)
    await session.flush()
    session.add(UserRole(user_id=user.id, role=Role.USER))
    await session.flush()
    return user


def _headers(user: User) -> dict[str, str]:
    token = create_token(user.id, Role.USER, "access", {"roles": [Role.USER.value]})
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Floods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_address_is_refused_past_its_allowance_for_the_minute(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    codes = [(await client.get(f"{API}/programs?size=1")).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    refused = await client.get(f"{API}/programs?size=1")
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "60"
    assert refused.json()["code"] == "rate_limited"
    # The probes the platform's own monitoring calls are never throttled.
    assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_the_expensive_paths_have_a_tighter_ceiling(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 100)
    monkeypatch.setattr(settings, "expensive_rate_limit_per_minute", 2)
    codes = [(await client.get(f"{API}/vacancies/meta")).status_code for _ in range(3)]
    assert codes[2] == 429
    # Ordinary paths are untouched by the expensive ceiling.
    assert (await client.get(f"{API}/programs?size=1")).status_code == 200


# ---------------------------------------------------------------------------
# Races
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_tabs_spending_one_refresh_token_get_one_session(client, session):
    registered = await client.post(
        f"{API}/auth/register",
        json={
            "email": "race@example.uz",
            "password": "long-enough-password",
            "accepted_privacy_policy": True,
        },
        headers={"X-Refresh-Transport": "body"},
    )
    refresh = registered.json()["refresh_token"]
    # Sequentially rather than concurrently: the test client shares one
    # database session, so true parallelism would serialise on it anyway. The
    # property under test is that the *second* spend of a token — however it
    # arrives — is refused because the revocation row already exists.
    first = await client.post(f"{API}/auth/refresh", json={"refresh_token": refresh})
    second = await client.post(f"{API}/auth/refresh", json={"refresh_token": refresh})
    assert first.status_code == 200
    assert second.status_code == 401
    # One row: the spent token. The account-wide consequence of the replay is
    # a bumped session generation on the user, not another row.
    reasons = sorted((await session.scalars(select(RevokedToken.reason))).all())
    assert reasons == ["rotated"]


@pytest.mark.asyncio
async def test_a_spent_token_is_a_single_row_whatever_the_order(session):
    """The revocation itself is atomic: the unique index decides the winner."""
    claims = {"jti": "the-same-jti", "sub": str(uuid.uuid4()), "exp": 4_102_444_800}
    from app.services import token_revocation

    first = await token_revocation.revoke(session, claims, reason="rotated")
    second = await token_revocation.revoke(session, claims, reason="rotated")
    assert (first, second) == (True, False)


# ---------------------------------------------------------------------------
# Security events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_sign_in_is_logged_without_the_password_or_the_address(client, caplog):
    with caplog.at_level(logging.WARNING, logger="security"):
        response = await client.post(
            f"{API}/auth/login",
            json={"email": "someone@example.uz", "password": "wrong-password-1"},
        )
    assert response.status_code == 401
    events = [record for record in caplog.records if record.name == "security"]
    assert events and events[-1].extra_fields["event"] == "auth.login_failed"
    assert events[-1].extra_fields["identifier"] == "s******@example.uz"
    assert "wrong-password-1" not in caplog.text
    assert "someone@example.uz" not in caplog.text


@pytest.mark.asyncio
async def test_a_revoked_token_presented_again_is_an_event(client, session, caplog):
    user = await _user(session)
    headers = _headers(user)
    await client.post(f"{API}/auth/logout", json={}, headers=headers)
    with caplog.at_level(logging.WARNING, logger="security"):
        assert (await client.get(f"{API}/users/me", headers=headers)).status_code == 401
    assert any(
        record.extra_fields.get("event") == "auth.revoked_token_used"
        for record in caplog.records
        if record.name == "security"
    )


# ---------------------------------------------------------------------------
# Retention and erasure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sweep_blanks_old_excerpts_and_drops_spent_bookkeeping(session, monkeypatch):
    monkeypatch.setattr(settings, "ai_trace_retention_days", 30)
    user = await _user(session)
    now = datetime.now(UTC)
    old = AiInteraction(
        user_id=user.id,
        feature="navigator",
        model_version="m",
        prompt_version="p",
        trace_id="old",
        prompt_excerpt="Savolim bor",
        response_excerpt="Javob",
    )
    recent = AiInteraction(
        user_id=user.id,
        feature="navigator",
        model_version="m",
        prompt_version="p",
        trace_id="recent",
        prompt_excerpt="Yangi savol",
        response_excerpt="Yangi javob",
    )
    session.add_all(
        [
            old,
            recent,
            OtpChallenge(identifier="x@example.uz", code_hash="h", salt="s", expires_at=now),
            RevokedToken(jti="gone", reason="signed_out", expires_at=now - timedelta(days=1)),
            RevokedToken(jti="live", reason="signed_out", expires_at=now + timedelta(days=1)),
            AuthThrottle(bucket="b", hits=1, expires_at=now - timedelta(hours=1)),
        ]
    )
    await session.flush()
    old.created_at = now - timedelta(days=40)
    await session.flush()
    spent = await session.scalar(
        select(OtpChallenge).where(OtpChallenge.identifier == "x@example.uz")
    )
    spent.created_at = now - timedelta(days=2)
    await session.flush()

    purged = await retention.purge(session, now=now)
    assert (
        purged.ai_excerpts,
        purged.otp_challenges,
        purged.revoked_tokens,
        purged.throttle_rows,
    ) == (
        1,
        1,
        1,
        1,
    )
    await session.refresh(old)
    await session.refresh(recent)
    # The trace stays; only the text goes.
    assert old.prompt_excerpt is None and old.response_excerpt is None
    assert old.trace_id == "old" and old.model_version == "m"
    assert recent.prompt_excerpt == "Yangi savol"
    assert (await session.scalars(select(RevokedToken.jti))).all() == ["live"]


@pytest.mark.asyncio
async def test_erasing_an_account_takes_her_conversations_and_codes_with_it(client, session):
    user = await _user(session)
    session.add(
        AiInteraction(
            user_id=user.id,
            feature="navigator",
            model_version="m",
            prompt_version="p",
            trace_id="t",
            prompt_excerpt="Mening savolim",
            response_excerpt="Javob",
        )
    )
    session.add(
        OtpChallenge(identifier=user.email, code_hash="h", salt="s", expires_at=datetime.now(UTC))
    )
    await session.flush()

    response = await client.delete(f"{API}/users/me", headers=_headers(user))
    assert response.status_code == 200
    trace = await session.scalar(select(AiInteraction).where(AiInteraction.user_id == user.id))
    assert trace is not None and trace.prompt_excerpt is None and trace.response_excerpt is None
    assert (
        await session.scalar(
            select(func.count())
            .select_from(OtpChallenge)
            .where(OtpChallenge.identifier == user.email)
        )
    ) == 0
    await session.refresh(user)
    assert user.email is None and user.status == UserStatus.DELETED
    # And the token she used is dead with the account.
    assert (await client.get(f"{API}/users/me", headers=_headers(user))).status_code == 401
