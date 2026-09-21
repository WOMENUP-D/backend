"""Open a desk account from the server console.

Until now the only thing that ever created an administrator was the
demonstration seed, and the seed refuses to run against production — its
password is published in this repository. So a freshly deployed installation
has a working `/admin` and nobody who can sign in to it.

This is the supported way to open the first one, and to hand out a
coordinator's or moderator's account afterwards:

    WOMANUP_STAFF_PASSWORD='…' python -m app.create_staff --email a@womanup.uz

The password is read from the environment or asked for on a hidden prompt —
never from the command line, where it would sit in the shell history and in
`ps` — and it is never printed back. Granting the role is an administrative
act, so it leaves an `AuditLog` row like any other.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import UTC, datetime
from getpass import getpass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import DataClassification, Region, Role, UserStatus
from app.core.security import hash_password
from app.db import SessionLocal, engine
from app.models.audit import AuditLog
from app.models.profile import Profile
from app.models.user import User, UserRole
from app.services.auth_service import STAFF_ROLES

#: Where the password comes from when the console is not interactive.
PASSWORD_VARIABLE = "WOMANUP_STAFF_PASSWORD"

#: Short enough to type at a desk, long enough not to be guessed at 10 000
#: attempts an hour. The sign-in screen refuses anything under 8, so this is
#: deliberately stricter for the accounts that can read everyone's data.
MINIMUM_PASSWORD_LENGTH = 12

_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


class CommandError(Exception):
    """Something the operator can fix; printed without a traceback."""


def clean_email(value: str) -> str:
    address = value.strip().lower()
    if not _EMAIL.fullmatch(address) or len(address) > 255:
        raise CommandError("--email must be an e-mail address")
    return address


def check_password(password: str, email: str) -> None:
    """Refuse what would make the account worse than no account at all."""
    if password != password.strip():
        raise CommandError(
            f"{PASSWORD_VARIABLE} begins or ends with a space — "
            "probably not what was meant, and impossible to type at sign-in"
        )
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        raise CommandError(f"the password must be at least {MINIMUM_PASSWORD_LENGTH} characters")
    local_part = email.split("@")[0]
    if local_part and local_part in password.lower():
        raise CommandError("the password must not contain the address it belongs to")
    if password.lower().startswith("womanup"):
        # The demonstration password, and every variation anyone reaches for
        # after reading it in the repository.
        raise CommandError("the password must not be built from the project name")


def read_password(email: str) -> str:
    """From the environment, or a hidden prompt when somebody is watching."""
    password = os.environ.get(PASSWORD_VARIABLE)
    if password is None:
        if not sys.stdin.isatty():
            raise CommandError(f"set {PASSWORD_VARIABLE} (this console cannot ask for it)")
        password = getpass("Password: ")
        if password != getpass("Repeat: "):
            raise CommandError("the two passwords are not the same")
    check_password(password, email)
    return password


def parse_role(value: str) -> Role:
    try:
        role = Role(value)
    except ValueError:
        role = None  # type: ignore[assignment]
    if role not in STAFF_ROLES:
        allowed = ", ".join(sorted(r.value for r in STAFF_ROLES))
        raise CommandError(f"--role must be one of: {allowed}")
    return role


def parse_region(value: str | None, role: Role) -> Region | None:
    if value is None:
        return None
    if role is not Role.REGIONAL_COORDINATOR:
        raise CommandError("--region belongs to a regional_coordinator account")
    try:
        return Region(value.strip().lower())
    except ValueError as exc:
        raise CommandError(f"unknown region: {', '.join(r.value for r in Region)}") from exc


async def create_staff_account(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    role: Role,
    full_name: str | None = None,
    region: Region | None = None,
    update: bool = False,
) -> tuple[User, bool]:
    """Create the account, or update an existing one when asked to.

    Returns the account and whether it was created just now. Flushes but does
    not commit: the caller decides, and the tests keep their transaction.
    """
    user = await session.scalar(select(User).where(func.lower(User.email) == email))
    created = user is None

    if user is not None and not update:
        raise CommandError(
            "an account with this address already exists; "
            "pass --update to set its password and grant the role"
        )

    now = datetime.now(UTC)
    if user is None:
        user = User(email=email, auth_provider="password")
        session.add(user)

    user.password_hash = hash_password(password)
    user.email_verified = True
    user.status = UserStatus.ACTIVE
    await session.flush()

    granted = await session.scalar(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role == role)
    )
    if granted is None:
        granted = UserRole(user_id=user.id, role=role)
        session.add(granted)
    granted.scope_region = region

    if full_name:
        profile = await session.scalar(select(Profile).where(Profile.user_id == user.id))
        if profile is None:
            profile = Profile(user_id=user.id)
            session.add(profile)
        profile.full_name = full_name

    session.add(
        AuditLog(
            # No actor: this ran on the server console, not through the API.
            actor_role=role.value,
            action="staff_account.created" if created else "staff_account.updated",
            entity_type="user",
            entity_id=str(user.id),
            classification=DataClassification.PERSONAL,
            changes={
                "role": role.value,
                "region": region.value if region else None,
                "via": "console",
                "at": now.isoformat(),
            },
        )
    )
    await session.flush()
    return user, created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.create_staff",
        description="Create or update a staff account. The password comes from "
        f"{PASSWORD_VARIABLE} or a hidden prompt.",
    )
    parser.add_argument("--email", required=True, help="the sign-in address")
    parser.add_argument(
        "--role",
        default=Role.ADMIN.value,
        help="admin (default), regional_coordinator, moderator or trainer",
    )
    parser.add_argument("--name", default=None, help="full name, shown in the panel")
    parser.add_argument("--region", default=None, help="scope for a regional_coordinator")
    parser.add_argument(
        "--update",
        action="store_true",
        help="the address may already exist: set its password and grant the role",
    )
    return parser


async def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        email = clean_email(arguments.email)
        role = parse_role(arguments.role)
        region = parse_region(arguments.region, role)
        password = read_password(email)
    except CommandError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2

    try:
        async with SessionLocal() as session:
            user, created = await create_staff_account(
                session,
                email=email,
                password=password,
                role=role,
                full_name=arguments.name,
                region=region,
                update=arguments.update,
            )
            await session.commit()
            account_id = user.id
    except CommandError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()

    print(f"{'Created' if created else 'Updated'} {email} — role {role.value}, status active.")
    print(f"WomanUP ID: {account_id}")
    print("Sign in at /admin/login with this address and the password you set.")
    return 0


def run() -> None:
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    run()
