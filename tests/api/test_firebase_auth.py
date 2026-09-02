"""Signing in with Google: account creation, recognition and non-duplication.

Google's own verification is covered in `tests/services/test_google_identity`;
here the identity is taken as already established, and what is under test is
what the portal does with it — that a second sign-in finds the same WomanUP ID
rather than opening another one, and that a desk account cannot be taken over
by whoever can register the matching Gmail address.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.api import auth as auth_api
from app.core.constants import Role, UserStatus
from app.core.security import hash_password
from app.models.profile import Profile
from app.models.user import User, UserRole
from app.services import firebase_auth
from app.services.firebase_auth import (
    FirebaseAuthError,
    FirebaseUnavailable,
    GoogleIdentity,
)

DILNOZA = GoogleIdentity(
    subject="firebase-uid-1029384756",
    email="dilnoza@gmail.com",
    given_name="Dilnoza",
    family_name="Karimova",
    picture="https://lh3.googleusercontent.com/a/photo",
)


@pytest.fixture(autouse=True)
def fresh_throttle():
    """The IP throttle lives for the life of the process; tests must not
    inherit each other's failures."""
    auth_api._google_throttle = auth_api.IpThrottle(limit=15)


@pytest.fixture
def google_says(monkeypatch):
    """Stand in for Google. The exchange itself is tested elsewhere."""

    def _use(identity: GoogleIdentity | Exception):
        async def fake(_id_token: str) -> GoogleIdentity:
            if isinstance(identity, Exception):
                raise identity
            return identity

        monkeypatch.setattr(firebase_auth, "verify", fake)

    return _use


async def sign_in(client, id_token: str = "a-firebase-id-token-long-enough-to-pass"):
    return await client.post("/api/v1/auth/google", json={"id_token": id_token})


async def test_first_sign_in_opens_an_account(client, session, google_says):
    google_says(DILNOZA)
    response = await sign_in(client)

    assert response.status_code == 200
    body = response.json()
    assert body["is_new_user"] is True
    # Google supplies no age and no region, so she still has to finish
    # onboarding before the cabinet means anything.
    assert body["onboarding_completed"] is False
    assert body["access_token"] and body["refresh_token"]

    user = await session.scalar(select(User).where(User.firebase_uid == DILNOZA.subject))
    assert user is not None
    assert user.email == "dilnoza@gmail.com"
    assert user.email_verified is True
    assert user.auth_provider == "google"
    assert {a.role for a in user.roles} == {Role.USER}


async def test_the_name_and_photo_google_gave_us_seed_the_profile(client, session, google_says):
    google_says(DILNOZA)
    await sign_in(client)

    profile = await session.scalar(
        select(Profile).join(User, User.id == Profile.user_id).where(User.email == DILNOZA.email)
    )
    assert profile is not None
    assert profile.full_name == "Dilnoza Karimova"
    assert profile.avatar_url == DILNOZA.picture


async def test_signing_in_twice_does_not_open_a_second_account(client, session, google_says):
    google_says(DILNOZA)
    first = (await sign_in(client)).json()
    second = (await sign_in(client)).json()

    assert first["is_new_user"] is True
    assert second["is_new_user"] is False
    assert await session.scalar(select(func.count(User.id))) == 1


async def test_a_changed_google_address_still_finds_the_same_account(client, session, google_says):
    """Google lets its owner rename the mailbox; the Firebase uid does not."""
    google_says(DILNOZA)
    await sign_in(client)

    renamed = replace(DILNOZA, email="dilnoza.karimova@gmail.com")
    google_says(renamed)
    again = (await sign_in(client)).json()

    assert again["is_new_user"] is False
    assert await session.scalar(select(func.count(User.id))) == 1


async def test_an_existing_learner_is_linked_rather_than_duplicated(client, session, google_says):
    existing = User(
        email="dilnoza@gmail.com",
        status=UserStatus.ACTIVE,
        email_verified=True,
        auth_provider="otp",
    )
    session.add(existing)
    await session.flush()
    session.add(UserRole(user_id=existing.id, role=Role.USER))
    await session.flush()

    google_says(DILNOZA)
    body = (await sign_in(client)).json()

    assert body["is_new_user"] is False
    assert await session.scalar(select(func.count(User.id))) == 1
    await session.refresh(existing)
    assert existing.firebase_uid == DILNOZA.subject


async def test_a_woman_who_registered_by_phone_keeps_her_own_account(client, session, google_says):
    """A phone account carries no address, so there is nothing to match on and
    nothing to hijack: she gets a separate WomanUP ID, not somebody else's."""
    by_phone = User(phone="+998901112233", status=UserStatus.ACTIVE, phone_verified=True)
    session.add(by_phone)
    await session.flush()
    session.add(UserRole(user_id=by_phone.id, role=Role.USER))
    await session.flush()

    google_says(DILNOZA)
    body = (await sign_in(client)).json()

    assert body["is_new_user"] is True
    assert await session.scalar(select(func.count(User.id))) == 2


async def test_a_staff_account_cannot_be_taken_over_through_google(client, session, google_says):
    """Otherwise anyone able to register the matching Gmail address would walk
    into the dashboard. Desk accounts keep their password."""
    admin = User(
        email="dilnoza@gmail.com",
        password_hash=hash_password("a-real-password"),
        status=UserStatus.ACTIVE,
        email_verified=True,
    )
    session.add(admin)
    await session.flush()
    session.add(UserRole(user_id=admin.id, role=Role.ADMIN))
    await session.flush()

    google_says(DILNOZA)
    response = await sign_in(client)

    assert response.status_code == 409
    await session.refresh(admin)
    assert admin.firebase_uid is None
    assert await session.scalar(select(func.count(User.id))) == 1


async def test_a_suspended_account_is_refused(client, session, google_says):
    suspended = User(
        email=DILNOZA.email,
        firebase_uid=DILNOZA.subject,
        status=UserStatus.SUSPENDED,
        email_verified=True,
    )
    session.add(suspended)
    await session.flush()
    session.add(UserRole(user_id=suspended.id, role=Role.USER))
    await session.flush()

    google_says(DILNOZA)
    assert (await sign_in(client)).status_code == 409


async def test_a_rejected_identity_never_reaches_the_database(client, session, google_says):
    google_says(FirebaseAuthError("invalid Google sign-in"))
    response = await sign_in(client)

    assert response.status_code == 401
    assert await session.scalar(select(func.count(User.id))) == 0


async def test_google_being_unreachable_is_not_reported_as_a_bad_account(client, google_says):
    google_says(FirebaseUnavailable("could not reach Google"))
    assert (await sign_in(client)).status_code == 503


async def test_the_finished_onboarding_flag_is_reported(client, session, google_says):
    """The sign-in screen routes on this, so it has to reflect the account."""
    from datetime import UTC, datetime

    done = User(
        email=DILNOZA.email,
        firebase_uid=DILNOZA.subject,
        status=UserStatus.ACTIVE,
        email_verified=True,
        onboarding_completed_at=datetime.now(UTC),
    )
    session.add(done)
    await session.flush()
    session.add(UserRole(user_id=done.id, role=Role.USER))
    await session.flush()

    google_says(DILNOZA)
    assert (await sign_in(client)).json()["onboarding_completed"] is True


async def test_repeated_failures_are_throttled(client, google_says):
    google_says(FirebaseAuthError("invalid Google sign-in"))
    for _ in range(15):
        assert (await sign_in(client)).status_code == 401
    assert (await sign_in(client)).status_code == 429


async def test_the_request_must_carry_a_token(app_client):
    assert (await app_client.post("/api/v1/auth/google", json={})).status_code == 422
    # Too short to be an ID token: rejected before anything reaches Firebase.
    short = await app_client.post("/api/v1/auth/google", json={"id_token": "nope"})
    assert short.status_code == 422
