"""Organisations: onboarding, membership, and the profile they choose to show.

**Roles are the ones the platform already has.** An organisation's people hold
PARTNER (an employer, an investor, an NGO, a government body) or TRAINER (an
education provider). Membership says *which* organisation they act for; it is
not a new kind of account, and a member whose role is later revoked can no
longer act for anybody.

**Every privileged act is audited** — creating, verifying or suspending an
organisation, adding or removing a member, and everything a member does to a
listing or an application (see `services.employer`).

**Nothing is guessed.** An organisation exists because an administrator
onboarded it. Its profile says only what its owner wrote, and its public page
shows a fixed set of fields, only after the owner publishes it.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    DataClassification,
    OrganizationKind,
    OrgMemberRole,
    Region,
    Role,
)
from app.models.opportunity import Opportunity
from app.models.organization import Organization, OrganizationMember
from app.models.program import Program
from app.models.user import User, UserRole
from app.schemas.auth import CurrentUser
from app.services import eligibility
from app.services.audit_service import record_audit

#: The existing role an organisation's people act under.
ROLE_FOR_KIND: dict[OrganizationKind, Role] = {
    OrganizationKind.EMPLOYER: Role.PARTNER,
    OrganizationKind.INVESTOR: Role.PARTNER,
    OrganizationKind.NGO: Role.PARTNER,
    OrganizationKind.GOVERNMENT: Role.PARTNER,
    OrganizationKind.EDUCATION_PROVIDER: Role.TRAINER,
}
MEMBER_ROLES = (Role.PARTNER, Role.TRAINER)
MAX_DESCRIPTION = 2000


class OrgError(Exception):
    """A refused organisation action. `reason` is a key the page words."""

    def __init__(self, reason: str, status_code: int = 422) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Onboarding (administrators)
# ---------------------------------------------------------------------------


def _slug_base(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "tashkilot")[:60]


async def _unique_slug(session: AsyncSession, name: str) -> str:
    base = _slug_base(name)
    slug, n = base, 2
    while await session.scalar(select(Organization.id).where(Organization.slug == slug)):
        slug = f"{base}-{n}"
        n += 1
    return slug


async def create(
    session: AsyncSession, *, name: str, kind: OrganizationKind, admin: CurrentUser
) -> Organization:
    name = name.strip()
    if len(name) < 2:
        raise OrgError("name")
    org = Organization(
        slug=await _unique_slug(session, name),
        name=name,
        kind=kind,
        created_by_id=uuid.UUID(admin.id),
    )
    session.add(org)
    await session.flush()
    await _audit(session, admin, "organization.create", org, {"name": name, "kind": kind.value})
    return org


async def set_status(
    session: AsyncSession,
    org: Organization,
    *,
    admin: CurrentUser,
    verified: bool | None = None,
    active: bool | None = None,
) -> Organization:
    changes: dict = {}
    if verified is not None and verified != org.is_verified:
        org.is_verified = verified
        org.verified_at = datetime.now(UTC) if verified else None
        changes["is_verified"] = verified
    if active is not None and active != org.is_active:
        org.is_active = active
        changes["is_active"] = active
        if not active:
            # A suspended organisation's listings stop everywhere at once —
            # the catalogue, the engine, the Coach, the career pages — by the
            # one flag they all already read. Reinstating the organisation
            # does not republish them; its people decide that.
            listings = (
                await session.scalars(
                    select(Opportunity).where(
                        Opportunity.organization_id == org.id, Opportunity.is_active.is_(True)
                    )
                )
            ).all()
            for listing in listings:
                listing.is_active = False
            changes["deactivated_listings"] = len(listings)
            # A reminder about an event that was taken down would announce
            # something that is no longer happening.
            from app.services import events

            dropped = await events.drop_reminders(session, [item.id for item in listings])
            if dropped:
                changes["dropped_reminders"] = dropped
    if changes:
        await _audit(session, admin, "organization.status", org, changes)
    await session.flush()
    return org


async def add_member(
    session: AsyncSession,
    org: Organization,
    *,
    email: str,
    role: OrgMemberRole,
    admin: CurrentUser,
) -> OrganizationMember:
    """Add an existing account as a member.

    The account must already exist — an organisation's person registers like
    anyone else. If it does not yet hold the role its organisation acts under,
    the administrator's action grants it, and that grant is audited like any
    other role assignment.
    """
    user = await session.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if user is None:
        raise OrgError("user_not_found", 404)

    existing = await session.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org.id,
            OrganizationMember.user_id == user.id,
        )
    )
    if existing is not None:
        existing.role = role
        await _audit(session, admin, "organization.member_role", org, {"role": role.value})
        await session.flush()
        return existing

    needed = ROLE_FOR_KIND[org.kind]
    held = await session.scalar(
        select(UserRole.id).where(UserRole.user_id == user.id, UserRole.role == needed)
    )
    if held is None:
        session.add(UserRole(user_id=user.id, role=needed, granted_by_id=uuid.UUID(admin.id)))
        await record_audit(
            session,
            action="role.assign",
            entity_type="user",
            entity_id=str(user.id),
            actor_id=uuid.UUID(admin.id),
            actor_role=admin.role.value,
            changes={"role": needed.value, "via": "organization", "organization_id": str(org.id)},
        )

    member = OrganizationMember(
        organization_id=org.id, user_id=user.id, role=role, added_by_id=uuid.UUID(admin.id)
    )
    session.add(member)
    await session.flush()
    await _audit(
        session, admin, "organization.member_add", org, {"member": str(user.id), "role": role.value}
    )
    return member


async def remove_member(
    session: AsyncSession, org: Organization, *, user_id: uuid.UUID, admin: CurrentUser
) -> None:
    member = await session.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org.id, OrganizationMember.user_id == user_id
        )
    )
    if member is None:
        raise OrgError("member_not_found", 404)
    await session.delete(member)
    await session.flush()
    await _audit(session, admin, "organization.member_remove", org, {"member": str(user_id)})


# ---------------------------------------------------------------------------
# Who may act for an organisation
# ---------------------------------------------------------------------------


async def membership(
    session: AsyncSession, user: CurrentUser, org_id: uuid.UUID
) -> tuple[Organization, OrganizationMember] | None:
    """Her membership of an active organisation — or None.

    Three things must all hold, every time: the membership row exists, the
    organisation is active, and she still holds — in the database, not merely
    in her token — the role that organisation's people act under. Revoking her
    PARTNER or TRAINER role ends what she can do for every organisation at once.
    """
    if not user.has_role(*MEMBER_ROLES):
        return None
    row = (
        await session.execute(
            select(Organization, OrganizationMember)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(
                Organization.id == org_id,
                Organization.is_active.is_(True),
                OrganizationMember.user_id == uuid.UUID(user.id),
            )
        )
    ).first()
    if row is None:
        return None
    if ROLE_FOR_KIND[row[0].kind] not in await _held_roles(session, uuid.UUID(user.id)):
        return None
    return row[0], row[1]


async def require_member(
    session: AsyncSession, user: CurrentUser, org_id: uuid.UUID, *, owner: bool = False
) -> tuple[Organization, OrganizationMember]:
    """The organisation she acts for, or a refusal.

    Not a member: 404, the same answer a non-existent organisation gets, so the
    endpoint cannot be used to learn which organisations exist. A member asking
    for an owner's action: 403.
    """
    found = await membership(session, user, org_id)
    if found is None:
        raise OrgError("not_found", 404)
    if owner and found[1].role != OrgMemberRole.OWNER:
        raise OrgError("owner_only", 403)
    return found


async def _held_roles(session: AsyncSession, user_id: uuid.UUID) -> set[Role]:
    """Her roles as they are now — read from the database, not the token, so a
    revoked role stops her at once rather than when her token expires."""
    rows = await session.scalars(select(UserRole.role).where(UserRole.user_id == user_id))
    return set(rows.all())


async def mine(
    session: AsyncSession, user: CurrentUser
) -> list[tuple[Organization, OrgMemberRole]]:
    """The organisations she acts for."""
    if not user.has_role(*MEMBER_ROLES):
        return []
    held = await _held_roles(session, uuid.UUID(user.id))
    rows = await session.execute(
        select(Organization, OrganizationMember.role)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            OrganizationMember.user_id == uuid.UUID(user.id),
            Organization.is_active.is_(True),
        )
        .order_by(Organization.name)
    )
    return [(org, role) for org, role in rows.all() if ROLE_FOR_KIND[org.kind] in held]


async def verified_org_for_partner(session: AsyncSession, user: CurrentUser) -> Organization | None:
    """The verified organisation a partner signs practical work for.

    A partner's verdict makes a skill *verified*, which is the strongest claim
    the platform records — so it has to be backed by an organisation WomanUP
    has checked, not just by a role on an account.
    """
    for org, _ in await mine(session, user):
        if org.is_verified and org.kind != OrganizationKind.EDUCATION_PROVIDER:
            return org
    return None


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------


def _url(value: str | None, *, https_only: bool = False) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    parsed = urlparse(value)
    allowed = ("https",) if https_only else ("http", "https")
    if parsed.scheme not in allowed or not parsed.netloc:
        raise OrgError("bad_url")
    return value[:500]


def _i18n(values: dict | None) -> dict:
    out: dict = {}
    for language in ("uz", "ru", "en"):
        text = (values or {}).get(language)
        if isinstance(text, str) and text.strip():
            out[language] = text.strip()[:MAX_DESCRIPTION]
    return out


async def update_profile(
    session: AsyncSession, org: Organization, *, changes: dict, actor: CurrentUser
) -> Organization:
    """An owner edits what her organisation shows. Name and kind are the
    administrator's: they are what the organisation was verified as."""
    applied: dict = {}
    if "description_i18n" in changes:
        org.description_i18n = _i18n(changes["description_i18n"])
        applied["description_i18n"] = True
    for field in ("industry", "city"):
        if field in changes:
            value = (changes[field] or "").strip() or None
            setattr(org, field, value[:120] if value else None)
            applied[field] = value
    if "region" in changes:
        org.region = Region(changes["region"]) if changes["region"] else None
        applied["region"] = changes["region"]
    if "website" in changes:
        org.website = _url(changes["website"])
        applied["website"] = org.website
    if "logo_url" in changes:
        # A picture loaded on every visitor's page: https only.
        org.logo_url = _url(changes["logo_url"], https_only=True)
        applied["logo_url"] = org.logo_url
    if "is_public" in changes:
        org.is_public = bool(changes["is_public"])
        applied["is_public"] = org.is_public
    await session.flush()
    await _audit(session, actor, "organization.profile", org, applied)
    return org


async def public_by_slug(session: AsyncSession, slug: str) -> Organization | None:
    """An organisation anyone may read: published by its owner, and active."""
    return await session.scalar(
        select(Organization).where(
            Organization.slug == slug[:80],
            Organization.is_public.is_(True),
            Organization.is_active.is_(True),
        )
    )


async def open_listings(session: AsyncSession, org: Organization) -> list[Opportunity]:
    rows = await session.execute(
        select(Opportunity)
        .where(Opportunity.organization_id == org.id, Opportunity.is_active.is_(True))
        .order_by(Opportunity.deadline.asc().nullslast())
    )
    return [item for item in rows.scalars() if eligibility.is_open(item)]


async def programmes(session: AsyncSession, org: Organization) -> list[Program]:
    rows = await session.execute(
        select(Program)
        .where(Program.organization_id == org.id, Program.is_published.is_(True))
        .order_by(Program.published_at.desc().nullslast())
    )
    return list(rows.scalars())


# ---------------------------------------------------------------------------


async def _audit(
    session: AsyncSession, actor: CurrentUser, action: str, org: Organization, changes: dict
) -> None:
    await record_audit(
        session,
        action=action,
        entity_type="organization",
        entity_id=str(org.id),
        actor_id=uuid.UUID(actor.id),
        actor_role=actor.role.value,
        classification=DataClassification.INTERNAL,
        changes=changes,
    )
