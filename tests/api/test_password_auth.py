"""Opening an account with a password, and coming back to it.

The sign-in code is gone, so the password is now the only thing standing
between a stranger and a woman's plan, her assistant history and her risk
flags. What is pinned here is that it actually stands: that a wrong password
is refused, that an account cannot be opened twice on one address, and that
neither endpoint can be read to discover who has an account.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.api import auth as auth_api
from app.core.constants import (
    PRIVACY_POLICY_VERSION,
    ConsentScope,
    Role,
    UserStatus,
)
from app.core.security import hash_password
from app.models.consent import ConsentLog
from app.models.user import User, UserRole

EMAIL = "dilnoza@example.com"
PASSWORD = "olma-daraxti-2026"


@pytest.fixture(autouse=True)
def fresh_throttles():
    """The throttles live for the life of the process; tests must not inherit
    each other's failures."""
    auth_api._login_throttle = auth_api.IpThrottle(limit=8)
    auth_api._register_throttle = auth_api.IpThrottle(limit=6, window=300.0)


async def register(client, email=EMAIL, password=PASSWORD, **extra):
    """Agreeing to the privacy policy is part of opening an account, so the
    happy path always carries it; tests that probe the requirement pass their
    own value through `extra`."""
    body = {"email": email, "password": password, "accepted_privacy_policy": True}
    body.update(extra)
    return await client.post("/api/v1/auth/register", json=body)


async def login(client, email=EMAIL, password=PASSWORD):
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def make_user(session, email=EMAIL, password=PASSWORD, status=UserStatus.ACTIVE):
    user = User(
        email=email,
        password_hash=hash_password(password) if password else None,
        status=status,
        auth_provider="password",
    )
    session.add(user)
    await session.flush()
    session.add(UserRole(user_id=user.id, role=Role.USER))
    await session.flush()
    return user


async def test_registering_opens_an_account_and_signs_her_straight_in(client, session):
    response = await register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["access_token"] and body["refresh_token"]

    user = await session.scalar(select(User).where(User.email == EMAIL))
    assert user is not None
    assert user.auth_provider == "password"
    assert {a.role for a in user.roles} == {Role.USER}


async def test_the_password_is_never_stored_as_typed(client, session):
    await register(client)
    user = await session.scalar(select(User).where(User.email == EMAIL))
    assert user.password_hash and PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2")


async def test_the_address_is_matched_regardless_of_case(client, session):
    await register(client, email="Dilnoza@Example.COM")
    assert await session.scalar(select(User).where(User.email == EMAIL)) is not None
    assert (await login(client, email="DILNOZA@example.com")).status_code == 200


async def test_one_address_cannot_open_two_accounts(client, session):
    assert (await register(client)).status_code == 201
    second = await register(client, password="a-different-password")

    assert second.status_code == 409
    assert await session.scalar(select(func.count(User.id))) == 1


async def test_a_short_password_is_refused_before_it_reaches_the_database(client, session):
    assert (await register(client, password="1234567")).status_code == 422
    assert await session.scalar(select(func.count(User.id))) == 0


async def test_she_can_come_back_with_her_password(client, session):
    await make_user(session)
    assert (await login(client)).status_code == 200


async def test_a_wrong_password_is_refused(client, session):
    await make_user(session)
    assert (await login(client, password="not-the-password")).status_code == 401


async def test_an_unknown_address_and_a_wrong_password_look_identical(client, session):
    """Otherwise the screen becomes a way to ask whether a woman is registered
    on a women's portal — which for some of them is itself sensitive."""
    await make_user(session)
    unknown = await login(client, email="nobody@example.com")
    wrong = await login(client, password="not-the-password")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_an_account_with_no_password_cannot_be_walked_into(client, session):
    """Accounts opened before passwords existed have no hash. An empty hash
    must not mean an empty door."""
    await make_user(session, password=None)
    assert (await login(client, password="anything")).status_code == 401


async def test_a_suspended_account_is_refused(client, session):
    await make_user(session, status=UserStatus.SUSPENDED)
    assert (await login(client)).status_code == 401


async def test_guessing_is_throttled(client, session):
    await make_user(session)
    for _ in range(8):
        assert (await login(client, password="guess")).status_code == 401
    assert (await login(client, password="guess")).status_code == 429
    # The throttle is on the address-and-password endpoint, not on her account:
    # the right password from another address is still refused, not locked out.
    assert (await login(client)).status_code == 429


async def test_a_malformed_address_never_reaches_the_database(client, session):
    assert (await register(client, email="not-an-address")).status_code == 422
    assert await session.scalar(select(func.count(User.id))) == 0


@pytest.mark.asyncio
async def test_an_account_cannot_be_opened_without_agreeing_to_the_policy(client) -> None:
    """The tick is a precondition, not a preference. An account with no consent
    behind it is an account holding personal data with no lawful basis, so the
    schema refuses it rather than the interface merely discouraging it."""
    for omitted in ({}, {"accepted_privacy_policy": False}):
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "nopolicy@example.com", "password": PASSWORD, **omitted},
        )
        assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_agreeing_is_written_to_the_consent_log(client, session) -> None:
    """ "She agreed" is only a record if it says what she agreed to and when, and
    it lands in the same transaction as the account."""
    assert (await register(client)).status_code == 201

    user_id = await session.scalar(select(User.id).where(User.email == EMAIL))
    entries = list(
        (
            await session.execute(
                select(ConsentLog).where(
                    ConsentLog.user_id == user_id,
                    ConsentLog.scope == ConsentScope.PRIVACY_POLICY,
                )
            )
        ).scalars()
    )
    assert len(entries) == 1
    assert entries[0].accepted is True
    assert entries[0].policy_version == PRIVACY_POLICY_VERSION
    assert entries[0].accepted_at is not None
