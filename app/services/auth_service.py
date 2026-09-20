"""OTP registration and login."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.constants import Role, UserStatus
from app.core.security import (
    create_token,
    generate_otp,
    hash_otp,
    hash_password,
    verify_otp,
    verify_password,
)
from app.models.profile import Profile
from app.models.user import OtpChallenge, User, UserRole
from app.schemas.auth import TokenPair
from app.services.firebase_auth import GoogleIdentity


class AuthError(Exception):
    """Authentication failure with a user-safe message."""


class OtpThrottled(AuthError):
    pass


class StaffOtpBlocked(AuthError):
    """A staff account asked for, or tried, a one-time code.

    Desks sign in with a login and a password on their own endpoint, which is
    throttled harder and refuses anything but an active staff account. Leaving
    the OTP door open for them would mean the strongest accounts on the
    platform could be reached by guessing six digits.
    """


#: The roles a woman using the portal holds. Anything else is a desk.
PARTICIPANT_ROLES = frozenset({Role.USER, Role.MOTHER})


def is_staff(user: User) -> bool:
    return bool(user.role_set - PARTICIPANT_ROLES)


async def _load_by_identifier(
    session: AsyncSession, identifier: str, is_email: bool
) -> User | None:
    field = User.email if is_email else User.phone
    return await session.scalar(
        select(User).options(selectinload(User.roles)).where(field == identifier)
    )


async def issue_otp(
    session: AsyncSession, identifier: str, purpose: str = "login", *, is_email: bool = True
) -> tuple[OtpChallenge, str]:
    """Create a challenge and return it with the clear-text code.

    The caller hands the code to the SMS/email sender and must never log it or
    return it over the API outside local development.

    Raises `StaffOtpBlocked` when the address belongs to a desk account, so no
    code is ever created for one.
    """
    now = datetime.now(UTC)

    existing = await _load_by_identifier(session, identifier, is_email)
    if existing is not None and is_staff(existing):
        raise StaffOtpBlocked("staff accounts sign in with a password")

    recent = await session.scalar(
        select(OtpChallenge)
        .where(
            OtpChallenge.identifier == identifier,
            OtpChallenge.consumed_at.is_(None),
        )
        .order_by(desc(OtpChallenge.created_at))
        .limit(1)
    )
    if recent is not None:
        age = (now - recent.created_at).total_seconds()
        if age < settings.otp_resend_cooldown_seconds:
            remaining = int(settings.otp_resend_cooldown_seconds - age)
            raise OtpThrottled(f"wait {remaining}s before requesting a new code")

    code = generate_otp()
    salt = secrets.token_hex(8)
    challenge = OtpChallenge(
        identifier=identifier,
        code_hash=hash_otp(code, salt),
        salt=salt,
        purpose=purpose,
        expires_at=now + timedelta(seconds=settings.otp_ttl_seconds),
    )
    session.add(challenge)
    await session.flush()
    return challenge, code


async def _count_failure(session: AsyncSession, challenge: OtpChallenge) -> None:
    """Record a wrong guess against the code, and block the code at the limit.

    Committed here, on purpose. The caller raises straight after, the router
    turns that into a 401, and the request's transaction is rolled back — so a
    counter left pending would be undone and the limit would never arrive.
    One statement does the counting and the blocking, which is also what makes
    it safe when several guesses land at once: each `UPDATE` reads the row's
    committed value under a row lock, so nothing is lost in a race.
    """
    attempts = await session.scalar(
        update(OtpChallenge)
        .where(OtpChallenge.id == challenge.id, OtpChallenge.consumed_at.is_(None))
        .values(
            attempts=OtpChallenge.attempts + 1,
            consumed_at=case(
                (
                    OtpChallenge.attempts + 1 >= settings.otp_max_attempts,
                    datetime.now(UTC),
                ),
                else_=None,
            ),
        )
        .returning(OtpChallenge.attempts)
    )
    await session.commit()
    if attempts is not None and attempts >= settings.otp_max_attempts:
        raise AuthError("too many attempts; request a new code")


async def verify_and_login(
    session: AsyncSession, identifier: str, code: str, is_email: bool
) -> TokenPair:
    """Verify an OTP, creating the user on first successful verification."""
    now = datetime.now(UTC)

    challenge = await session.scalar(
        select(OtpChallenge)
        .where(
            OtpChallenge.identifier == identifier,
            OtpChallenge.consumed_at.is_(None),
        )
        .order_by(desc(OtpChallenge.created_at))
        .limit(1)
    )
    if challenge is None:
        raise AuthError("no active verification code")
    if challenge.expires_at < now:
        raise AuthError("verification code expired")

    if not verify_otp(code, challenge.salt, challenge.code_hash):
        await _count_failure(session, challenge)
        raise AuthError("invalid verification code")

    # Consume the code in one statement, so two requests carrying the same
    # correct code cannot both be served: the second updates no row.
    consumed = await session.scalar(
        update(OtpChallenge)
        .where(OtpChallenge.id == challenge.id, OtpChallenge.consumed_at.is_(None))
        .values(consumed_at=now)
        .returning(OtpChallenge.id)
    )
    if consumed is None:
        raise AuthError("verification code already used")

    user = await _load_by_identifier(session, identifier, is_email)
    if user is not None and is_staff(user):
        raise StaffOtpBlocked("staff accounts sign in with a password")

    if user is None:
        user = User(
            email=identifier if is_email else None,
            phone=None if is_email else identifier,
            status=UserStatus.PENDING,
            email_verified=is_email,
            phone_verified=not is_email,
        )
        session.add(user)
        await session.flush()
        session.add(UserRole(user_id=user.id, role=Role.USER))
        await session.flush()
    else:
        if user.status == UserStatus.SUSPENDED:
            raise AuthError("account suspended")
        if is_email:
            user.email_verified = True
        else:
            user.phone_verified = True

    user.last_active_at = now
    await session.refresh(user)

    return _tokens_for(user)


class EmailTaken(AuthError):
    """That address already has an account."""


async def register_with_password(session: AsyncSession, email: str, password: str) -> User:
    """Open an account. Raises `EmailTaken` if the address is already in use.

    Unlike a failed sign-in, this one says plainly that the address is taken:
    she cannot act on "something went wrong", and whether an address has an
    account here is not a secret worth a woman being unable to register.
    """
    address = email.strip().lower()
    existing = await session.scalar(select(User).where(func.lower(User.email) == address))
    if existing is not None:
        raise EmailTaken("this address already has an account")

    user = User(
        email=address,
        password_hash=hash_password(password),
        status=UserStatus.PENDING,
        # The address is not proved yet — no code was sent. It becomes verified
        # when password recovery by e-mail is switched on.
        email_verified=False,
        auth_provider="password",
    )
    session.add(user)
    await session.flush()
    session.add(UserRole(user_id=user.id, role=Role.USER))
    await session.flush()
    await session.refresh(user)
    return user


async def authenticate_user(session: AsyncSession, email: str, password: str) -> User:
    """Verify an address and password. One error for every failure.

    Unknown address, wrong password, no password set, suspended — all the same
    message, so the sign-in screen cannot be used to discover who has an
    account. The hashing time is always spent, present or not, because
    returning instantly for an unknown address says just as much.
    """
    address = email.strip().lower()
    record = await session.scalar(
        select(User).where(func.lower(User.email) == address).options(selectinload(User.roles))
    )

    stored = record.password_hash if record and record.password_hash else DUMMY_HASH
    ok = verify_password(password, stored)

    if record is None or not ok:
        raise AuthError("invalid email or password")
    if record.status in (UserStatus.SUSPENDED, UserStatus.DELETED):
        raise AuthError("invalid email or password")

    record.last_active_at = datetime.now(UTC)
    return record


def issue_tokens(user: User) -> TokenPair:
    """Token pair for a caller that authenticated by some other route.

    The OTP flow reaches the private helper directly; staff sign-in goes
    through here so the API layer never touches a private name.
    """
    return _tokens_for(user)


def _tokens_for(user: User) -> TokenPair:
    role = user.primary_role
    claims = {"roles": [r.value for r in user.role_set]}
    return TokenPair(
        access_token=create_token(user.id, role, "access", claims),
        refresh_token=create_token(user.id, role, "refresh", claims),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


async def refresh_tokens(session: AsyncSession, user_id: str) -> TokenPair:
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None or user.status in (UserStatus.SUSPENDED, UserStatus.DELETED):
        raise AuthError("account is not active")
    return _tokens_for(user)


# Roles that sign in with a login and a password rather than a phone code.
# A coordinator works from a desk, often on a shared machine, and an SMS to a
# personal number is the wrong key for an office account.
STAFF_ROLES = (Role.ADMIN, Role.REGIONAL_COORDINATOR, Role.MODERATOR, Role.TRAINER)


# A real argon2 hash of a value nobody uses, so a failed lookup costs the same
# time as a wrong password.
DUMMY_HASH = hash_password("womanup-timing-guard")


async def authenticate_staff(session: AsyncSession, login: str, password: str) -> User:
    """Verify a staff login. Raises `AuthError` on every failure.

    One error for every case on purpose — unknown login, wrong password, not
    staff, suspended — so the response cannot be used to discover which
    accounts exist.
    """
    identifier = login.strip().lower()
    record = await session.scalar(
        select(User).where(func.lower(User.email) == identifier).options(selectinload(User.roles))
    )

    # Always spend the hashing time, present or not: returning instantly for an
    # unknown login tells an attacker the login is unknown.
    stored = record.password_hash if record and record.password_hash else DUMMY_HASH
    ok = verify_password(password, stored)

    if record is None or not ok:
        raise AuthError("invalid login or password")
    if record.status is not UserStatus.ACTIVE:
        raise AuthError("invalid login or password")
    if not any(role.role in STAFF_ROLES for role in record.roles):
        raise AuthError("invalid login or password")

    record.last_active_at = datetime.now(UTC)
    return record


# ------------------------------------------------------------ Google sign-in


async def login_with_google(session: AsyncSession, identity: GoogleIdentity) -> tuple[User, bool]:
    """Sign in or open an account from a *verified* Google identity.

    Returns the account and whether it was created just now.

    Matching order is deliberate. The Firebase uid first, because Google lets
    its owner change the address on an account and the uid never changes —
    matching on the address alone would hand one woman two WomanUP IDs the day
    she renames her mailbox. Then the verified address, so somebody who already
    registered here by e-mail is recognised rather than duplicated. Only then a
    new account.

    The caller must have verified the identity token first; nothing here
    re-checks it.
    """
    now = datetime.now(UTC)

    user = await session.scalar(
        select(User).where(User.firebase_uid == identity.subject).options(selectinload(User.roles))
    )
    created = False

    if user is None:
        user = await session.scalar(
            select(User)
            .where(func.lower(User.email) == identity.email)
            .options(selectinload(User.roles))
        )
        if user is not None:
            # A desk account is never linked by e-mail. Anyone who could open a
            # Google account on a coordinator's address would otherwise walk
            # into the dashboard, so staff keep the password they already have.
            if any(assignment.role in STAFF_ROLES for assignment in user.roles):
                raise AuthError("staff accounts sign in with a login and a password")
            user.firebase_uid = identity.subject
            user.email_verified = True

    if user is None:
        user = User(
            email=identity.email,
            firebase_uid=identity.subject,
            status=UserStatus.PENDING,
            email_verified=True,
            auth_provider="google",
        )
        session.add(user)
        await session.flush()
        session.add(UserRole(user_id=user.id, role=Role.USER))
        created = True

    if user.status in (UserStatus.SUSPENDED, UserStatus.DELETED):
        raise AuthError("account is not active")

    user.last_active_at = now
    await _fill_profile_from_google(session, user, identity)
    await session.flush()
    await session.refresh(user)
    return user, created


async def _fill_profile_from_google(
    session: AsyncSession, user: User, identity: GoogleIdentity
) -> None:
    """Seed the profile from Google — without overwriting anything she typed.

    Google's copy is a starting point, not the truth: a woman who corrected her
    name here should not find it reverted the next time she signs in.
    """
    profile = await session.scalar(select(Profile).where(Profile.user_id == user.id))
    if profile is None:
        profile = Profile(user_id=user.id)
        session.add(profile)

    if not profile.full_name and identity.full_name:
        profile.full_name = identity.full_name
    if not profile.avatar_url and identity.picture:
        profile.avatar_url = identity.picture
