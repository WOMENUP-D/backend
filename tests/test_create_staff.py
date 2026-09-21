"""Opening a desk account from the console.

Two things matter here. The account it makes must be one the staff sign-in
actually accepts — a production installation has no other way in — and the
command must refuse the passwords that would make that account worse than no
account at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.constants import DataClassification, Region, Role, UserStatus
from app.create_staff import (
    CommandError,
    check_password,
    clean_email,
    create_staff_account,
    parse_region,
    parse_role,
    read_password,
)
from app.models.audit import AuditLog
from app.models.profile import Profile
from app.models.user import User, UserRole
from app.services import auth_service

GOOD = "qorongu-osmon-2026"


# --------------------------------------------------------------- the console


def test_the_address_is_normalised():
    assert clean_email("  Admin@WomanUP.uz ") == "admin@womanup.uz"


@pytest.mark.parametrize("value", ["admin", "admin@localhost", "a b@womanup.uz", "@womanup.uz"])
def test_something_that_is_not_an_address_is_refused(value):
    with pytest.raises(CommandError):
        clean_email(value)


def test_only_a_desk_role_can_be_granted():
    assert parse_role("admin") is Role.ADMIN
    assert parse_role("regional_coordinator") is Role.REGIONAL_COORDINATOR
    for value in ("user", "mother", "partner", "mentor", "root", ""):
        with pytest.raises(CommandError):
            parse_role(value)


def test_a_region_scopes_a_coordinator_and_nobody_else():
    assert parse_region("andijan", Role.REGIONAL_COORDINATOR) is Region.ANDIJAN
    assert parse_region(None, Role.ADMIN) is None
    with pytest.raises(CommandError):
        parse_region("andijan", Role.ADMIN)
    with pytest.raises(CommandError):
        parse_region("atlantis", Role.REGIONAL_COORDINATOR)


@pytest.mark.parametrize(
    "password",
    [
        "short",  # under the minimum
        "WomanUP2026!",  # the published demonstration password
        "womanup-parol-2026",  # and everything built from the project name
        "admin-parol-2026",  # the address it belongs to
        " qorongu-osmon-2026",  # a stray space from a copied secret
    ],
)
def test_a_password_that_would_be_guessed_is_refused(password):
    with pytest.raises(CommandError):
        check_password(password, "admin@womanup.uz")


def test_a_good_password_passes():
    check_password(GOOD, "admin@womanup.uz")


def test_the_password_is_never_asked_for_on_the_command_line(monkeypatch):
    """It comes from the environment, or from a prompt nobody can read."""
    monkeypatch.setenv("WOMANUP_STAFF_PASSWORD", GOOD)
    assert read_password("admin@womanup.uz") == GOOD

    monkeypatch.delenv("WOMANUP_STAFF_PASSWORD")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(CommandError, match="WOMANUP_STAFF_PASSWORD"):
        read_password("admin@womanup.uz")


def test_an_environment_password_is_checked_too(monkeypatch):
    monkeypatch.setenv("WOMANUP_STAFF_PASSWORD", "WomanUP2026!")
    with pytest.raises(CommandError):
        read_password("admin@womanup.uz")


# ---------------------------------------------------------------- the account


@pytest.mark.asyncio
async def test_the_account_it_makes_can_sign_in(session):
    user, created = await create_staff_account(
        session,
        email="admin@womanup.uz",
        password=GOOD,
        role=Role.ADMIN,
        full_name="Durdona",
    )
    assert created
    assert user.status is UserStatus.ACTIVE
    assert user.email_verified
    assert user.auth_provider == "password"
    assert user.password_hash and GOOD not in user.password_hash

    # The point of the whole command: the staff sign-in accepts it.
    signed_in = await auth_service.authenticate_staff(session, "Admin@WomanUP.uz", GOOD)
    assert signed_in.id == user.id

    with pytest.raises(auth_service.AuthError):
        await auth_service.authenticate_staff(session, "admin@womanup.uz", "wrong-password-2026")

    profile = await session.scalar(select(Profile).where(Profile.user_id == user.id))
    assert profile is not None and profile.full_name == "Durdona"


@pytest.mark.asyncio
async def test_an_existing_address_is_left_alone_unless_asked(session):
    await create_staff_account(session, email="admin@womanup.uz", password=GOOD, role=Role.ADMIN)
    with pytest.raises(CommandError, match="--update"):
        await create_staff_account(
            session, email="admin@womanup.uz", password="boshqa-parol-2026", role=Role.ADMIN
        )


@pytest.mark.asyncio
async def test_update_sets_a_new_password_on_the_same_account(session):
    first, _ = await create_staff_account(
        session, email="admin@womanup.uz", password=GOOD, role=Role.ADMIN
    )
    again, created = await create_staff_account(
        session,
        email="admin@womanup.uz",
        password="boshqa-parol-2026",
        role=Role.ADMIN,
        update=True,
    )
    assert not created
    assert again.id == first.id, "a reset must not open a second WomanUP ID"

    await auth_service.authenticate_staff(session, "admin@womanup.uz", "boshqa-parol-2026")
    with pytest.raises(auth_service.AuthError):
        await auth_service.authenticate_staff(session, "admin@womanup.uz", GOOD)

    roles = (await session.scalars(select(UserRole).where(UserRole.user_id == first.id))).all()
    assert [role.role for role in roles] == [Role.ADMIN], "the role was granted twice"


@pytest.mark.asyncio
async def test_a_coordinator_is_scoped_to_her_region(session):
    user, _ = await create_staff_account(
        session,
        email="andijan@womanup.uz",
        password=GOOD,
        role=Role.REGIONAL_COORDINATOR,
        region=Region.ANDIJAN,
    )
    granted = await session.scalar(select(UserRole).where(UserRole.user_id == user.id))
    assert granted.role is Role.REGIONAL_COORDINATOR
    assert granted.scope_region is Region.ANDIJAN


@pytest.mark.asyncio
async def test_the_grant_is_written_to_the_audit_log(session):
    user, _ = await create_staff_account(
        session, email="admin@womanup.uz", password=GOOD, role=Role.ADMIN
    )
    entry = await session.scalar(select(AuditLog).where(AuditLog.entity_id == str(user.id)))
    assert entry is not None
    assert entry.action == "staff_account.created"
    assert entry.entity_type == "user"
    assert entry.classification is DataClassification.PERSONAL
    assert entry.changes["role"] == Role.ADMIN.value
    assert entry.changes["via"] == "console"
    assert GOOD not in str(entry.changes), "the password reached the audit log"


@pytest.mark.asyncio
async def test_a_learner_account_is_promoted_rather_than_duplicated(session):
    """Somebody who already registered as a user, then joins the office."""
    learner = User(email="dilnoza@womanup.uz", status=UserStatus.PENDING)
    session.add(learner)
    await session.flush()
    session.add(UserRole(user_id=learner.id, role=Role.USER))
    await session.flush()

    promoted, created = await create_staff_account(
        session,
        email="dilnoza@womanup.uz",
        password=GOOD,
        role=Role.MODERATOR,
        update=True,
    )
    assert not created and promoted.id == learner.id
    roles = (await session.scalars(select(UserRole).where(UserRole.user_id == learner.id))).all()
    assert {role.role for role in roles} == {Role.USER, Role.MODERATOR}
    assert promoted.status is UserStatus.ACTIVE
