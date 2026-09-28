"""What a one-time code may and may not be used for.

The audit found the OTP path open to unlimited guessing: a wrong code
incremented a counter that the failed request then rolled back, so the
five-attempt limit never arrived, and nothing else stood in the way. These
tests hold that door shut — the counter survives the 401, the code blocks
itself, the endpoints are rate-limited per address and per IP, desk accounts
cannot be reached this way at all, and a code cannot be phoned for while there
is no SMS provider to phone it with.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.constants import Role, UserStatus
from app.models.rate_limit import AuthThrottle
from app.models.user import OtpChallenge, User, UserRole
from app.services import auth_service, mailer, rate_limit
from app.services.auth_service import AuthError

API = "/api/v1/auth"
REQUEST = f"{API}/otp/request"
VERIFY = f"{API}/otp/verify"
WRONG = "000000"


@pytest.fixture(autouse=True)
def code_sign_in_on(monkeypatch):
    """These tests describe the code path, so they switch it on.

    The portal signs people in with an address and a password, and codes are
    off by default (`test_code_sign_in_is_off_by_default`). The protections
    below still have to hold for the day the flow is wanted again.
    """
    monkeypatch.setattr(settings, "otp_enabled", True)


@pytest.fixture
def codes(monkeypatch) -> list[str]:
    """The codes the mailer was asked to send, instead of sending them.

    The test environment may have real SMTP credentials in `.env`; nothing
    here may put a message on the wire.
    """
    sent: list[str] = []

    async def capture(identifier: str, code: str, language: str = "uz") -> None:
        sent.append(code)

    monkeypatch.setattr("app.api.auth.mailer.send_code", capture)
    return sent


def address() -> str:
    return f"otp-{uuid.uuid4().hex[:10]}@example.uz"


async def ask(client, email: str, ip: str = "203.0.113.5") -> object:
    return await client.post(REQUEST, json={"email": email}, headers={"X-Forwarded-For": ip})


async def try_code(client, email: str, code: str, ip: str = "203.0.113.5") -> object:
    return await client.post(
        VERIFY, json={"email": email, "code": code}, headers={"X-Forwarded-For": ip}
    )


async def _challenge(session: AsyncSession, email: str) -> OtpChallenge | None:
    return await session.scalar(
        select(OtpChallenge)
        .where(OtpChallenge.identifier == email)
        .order_by(OtpChallenge.created_at.desc())
    )


async def _staff(session: AsyncSession, email: str, role: Role = Role.ADMIN) -> User:
    user = User(email=email, status=UserStatus.ACTIVE, email_verified=True)
    session.add(user)
    await session.flush()
    session.add(UserRole(user_id=user.id, role=role))
    await session.commit()
    return user


# ---------------------------------------------------------------------------
# The counter and the block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_wrong_code_is_counted_even_though_the_request_fails(client, session, codes):
    email = address()
    assert (await ask(client, email)).status_code == 200

    assert (await try_code(client, email, WRONG)).status_code == 401

    challenge = await _challenge(session, email)
    assert challenge is not None
    await session.refresh(challenge)
    # The 401 rolled the request back. The attempt is still on the record.
    assert challenge.attempts == 1


@pytest.mark.asyncio
async def test_the_code_blocks_itself_after_the_limit(client, session, codes):
    email = address()
    await ask(client, email)
    code = codes[0]

    for _ in range(settings.otp_max_attempts):
        assert (await try_code(client, email, WRONG)).status_code == 401

    challenge = await _challenge(session, email)
    await session.refresh(challenge)
    assert challenge.attempts == settings.otp_max_attempts
    assert challenge.consumed_at is not None

    # Further guesses find nothing to guess at — including the right one.
    assert (await try_code(client, email, WRONG)).status_code == 401
    assert (await try_code(client, email, code)).status_code == 401
    assert await session.scalar(select(func.count(User.id)).where(User.email == email)) == 0


@pytest.mark.asyncio
async def test_guesses_arriving_together_cannot_outrun_the_limit(engine, session, codes):
    """Ten guesses at once, one row: the database counts every one of them."""
    email = address()
    challenge, _ = await auth_service.issue_otp(session, email)
    await session.commit()
    challenge_id = challenge.id

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def guess() -> None:
        async with factory() as own:
            record = await own.get(OtpChallenge, challenge_id)
            # The guess that hits the limit says so; the others just count.
            with contextlib.suppress(AuthError):
                await auth_service._count_failure(own, record)

    await asyncio.gather(*(guess() for _ in range(10)))

    await session.refresh(challenge)
    assert challenge.attempts == settings.otp_max_attempts
    assert challenge.consumed_at is not None


@pytest.mark.asyncio
async def test_a_correct_code_works_once(client, session, codes):
    email = address()
    await ask(client, email)
    code = codes[0]

    first = await try_code(client, email, code)
    assert first.status_code == 200, first.text
    assert "access_token" in first.json()

    challenge = await _challenge(session, email)
    await session.refresh(challenge)
    assert challenge.consumed_at is not None

    # The same code a second time is worth nothing.
    assert (await try_code(client, email, code)).status_code == 401


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asking_for_codes_is_capped_per_address(client, monkeypatch, codes):
    monkeypatch.setattr(settings, "otp_resend_cooldown_seconds", 0)
    monkeypatch.setattr(settings, "otp_request_identifier_limit", 3)
    email = address()

    for attempt in range(3):
        # Each request comes from a different IP: the address is the cap here.
        assert (await ask(client, email, ip=f"198.51.100.{attempt}")).status_code == 200
    refused = await ask(client, email, ip="198.51.100.9")
    assert refused.status_code == 429


@pytest.mark.asyncio
async def test_asking_for_codes_is_capped_per_ip(client, monkeypatch, codes):
    monkeypatch.setattr(settings, "otp_request_ip_limit", 3)
    ip = "198.51.100.77"

    for _ in range(3):
        assert (await ask(client, address(), ip=ip)).status_code == 200
    assert (await ask(client, address(), ip=ip)).status_code == 429
    # Another address from another IP is unaffected.
    assert (await ask(client, address(), ip="198.51.100.78")).status_code == 200


@pytest.mark.asyncio
async def test_guessing_is_capped_per_ip_across_addresses(client, monkeypatch, codes):
    """A fresh code per address must not buy a fresh run of guesses."""
    monkeypatch.setattr(settings, "otp_verify_ip_limit", 4)
    ip = "198.51.100.31"

    refusals = 0
    for _ in range(6):
        email = address()
        await ask(client, email, ip="198.51.100.32")
        response = await try_code(client, email, WRONG, ip=ip)
        if response.status_code == 429:
            refusals += 1
    assert refusals >= 2


@pytest.mark.asyncio
async def test_guessing_is_capped_per_address_across_ips(client, monkeypatch, codes):
    monkeypatch.setattr(settings, "otp_verify_identifier_limit", 3)
    email = address()
    await ask(client, email)

    for attempt in range(3):
        assert (await try_code(client, email, WRONG, ip=f"192.0.2.{attempt}")).status_code == 401
    assert (await try_code(client, email, WRONG, ip="192.0.2.200")).status_code == 429


@pytest.mark.asyncio
async def test_the_resend_cooldown_still_applies(client, codes):
    email = address()
    assert (await ask(client, email)).status_code == 200
    assert (await ask(client, email)).status_code == 429


# ---------------------------------------------------------------------------
# Who may use this door
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_desk_account_gets_no_code_and_no_hint(client, session, codes):
    email = f"staff-{uuid.uuid4().hex[:8]}@womanup.uz"
    await _staff(session, email)

    response = await ask(client, email)
    # The same words a woman gets, so the reply cannot be used to find staff.
    assert response.status_code == 200
    assert response.json()["detail"] == "Verification code sent"
    assert codes == []
    assert await _challenge(session, email) is None

    # And with no code there is nothing to verify.
    assert (await try_code(client, email, WRONG)).status_code == 401


@pytest.mark.asyncio
async def test_an_account_made_staff_after_the_code_cannot_use_it(client, session, codes):
    email = address()
    await ask(client, email)
    code = codes[0]

    user = User(email=email, status=UserStatus.ACTIVE, email_verified=True)
    session.add(user)
    await session.flush()
    session.add(UserRole(user_id=user.id, role=Role.REGIONAL_COORDINATOR))
    await session.commit()

    response = await try_code(client, email, code)
    assert response.status_code == 403
    assert "password" in response.json()["detail"]


@pytest.mark.asyncio
async def test_phone_sign_in_is_closed_while_there_is_no_sms(client, session):
    phone = f"+9989{uuid.uuid4().int % 10**8:08d}"

    asked = await client.post(REQUEST, json={"phone": phone})
    assert asked.status_code == 503
    assert "e-mail" in asked.json()["detail"]

    tried = await client.post(VERIFY, json={"phone": phone, "code": WRONG})
    assert tried.status_code == 503

    # Nothing was created that a guess could land on.
    assert await _challenge(session, phone) is None
    assert await session.scalar(select(func.count(User.id)).where(User.phone == phone)) == 0


@pytest.mark.asyncio
async def test_a_code_is_never_returned_outside_local(client, monkeypatch):
    """Production must answer with words, never with the code itself."""

    async def unconfigured(identifier: str, code: str, language: str = "uz") -> None:
        raise mailer.MailNotConfigured("no smtp host")

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr("app.api.auth.mailer.send_code", unconfigured)

    response = await ask(client, address())
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail == "e-mail sending is not configured"
    assert not any(character.isdigit() for character in detail)


@pytest.mark.asyncio
async def test_a_brute_force_run_hits_a_ceiling(client, monkeypatch, codes):
    """The whole point, end to end.

    A six-digit code has a million values. An attacker who rotates IP
    addresses and keeps asking for fresh codes should still run out of tries:
    the per-address caps bound the number of guesses per window, whatever else
    changes. With the defaults that is 15 guesses an hour against one address,
    against the million values a code can take.
    """
    monkeypatch.setattr(settings, "otp_resend_cooldown_seconds", 0)
    monkeypatch.setattr(settings, "otp_request_identifier_limit", 4)
    monkeypatch.setattr(settings, "otp_verify_identifier_limit", 6)
    email = address()

    guesses = 0
    for attempt in range(40):
        ip = f"203.0.113.{attempt % 250}"
        await ask(client, email, ip=ip)
        response = await try_code(client, email, f"{attempt:06d}", ip=ip)
        if response.status_code == 429:
            break
        guesses += 1

    assert guesses <= settings.otp_verify_identifier_limit
    assert guesses < 40


@pytest.mark.asyncio
async def test_the_allowance_follows_the_address_whatever_its_capitalisation(
    client, monkeypatch, codes
):
    """`Victim@x.uz` and `victim@x.uz` must not be two runs of guesses."""
    monkeypatch.setattr(settings, "otp_verify_identifier_limit", 2)
    email = address()
    await ask(client, email)

    assert (await try_code(client, email, WRONG)).status_code == 401
    assert (await try_code(client, email.upper(), WRONG)).status_code == 401
    # The third guess is over the limit, however it is spelled.
    assert (await try_code(client, email.title(), WRONG)).status_code == 429


@pytest.mark.asyncio
async def test_the_throttle_table_keeps_no_address_and_no_ip(client, session, codes):
    """The counters are needed; who they count is not."""
    email = address()
    ip = "198.51.100.123"
    await ask(client, email, ip=ip)
    await try_code(client, email, WRONG, ip=ip)

    rows = (await session.execute(select(AuthThrottle))).scalars().all()
    assert rows, "the attempts were not counted at all"
    for row in rows:
        assert re.fullmatch(r"[0-9a-f]{64}", row.bucket), row.bucket
        assert row.expires_at is not None
    stored = " ".join(row.bucket for row in rows)
    assert email not in stored
    assert email.split("@")[0] not in stored
    assert ip not in stored


@pytest.mark.asyncio
async def test_requests_arriving_together_cannot_outrun_the_rate_limit(engine, session):
    """Twelve callers at once, an allowance of four: four get through."""
    limit = rate_limit.Limit("otp_verify_ip", 4, 3600)
    key = f"198.51.100.{uuid.uuid4().int % 250}"
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def call() -> bool:
        async with factory() as own:
            return await rate_limit.take(own, limit, key)

    allowed = await asyncio.gather(*(call() for _ in range(12)))
    assert sum(allowed) == 4
    assert sum(1 for outcome in allowed if not outcome) == 8

    hits = await session.scalar(select(func.count(AuthThrottle.id)))
    assert hits == 1, "one window, one row"


@pytest.mark.asyncio
async def test_code_sign_in_is_off_by_default(client, session, monkeypatch, codes):
    """The portal asks for an address and a password; nothing asks for a code."""
    monkeypatch.setattr(settings, "otp_enabled", False)
    email = address()

    asked = await ask(client, email)
    assert asked.status_code == 503
    assert "password" in asked.json()["detail"]

    tried = await try_code(client, email, WRONG)
    assert tried.status_code == 503

    # Nothing was created, and nothing was sent.
    assert await _challenge(session, email) is None
    assert codes == []
