"""Organisations on WomanUP: employers, education providers, investors, NGOs.

Three tables:

* `organizations` — the organisation and its profile. Created by a WomanUP
  administrator when an organisation is onboarded; never derived from a string
  on a listing, and never seeded. What its public page shows is a fixed set of
  fields, and only once the organisation publishes it (`is_public`).
* `organization_members` — who acts for it. A member must hold one of the
  roles the platform already has — PARTNER for an employer or an investor,
  TRAINER for an education provider. Membership adds *which* organisation, not
  a new kind of account.
* `organization_invitations` — an organisation asking a woman who opted into
  candidate discovery to consider it. Until she accepts, the organisation knows
  her only by a pseudonym that is different for every organisation.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import InvitationStatus, OrganizationKind, OrgMemberRole, Region
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum


class Organization(UUIDMixin, TimestampMixin, Base):
    """An organisation and the profile it chooses to show."""

    __tablename__ = "organizations"

    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[OrganizationKind] = mapped_column(str_enum(OrganizationKind, 30), nullable=False)

    # The public profile. Filled in by the organisation; nothing here is
    # guessed or copied from a listing.
    description_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    industry: Mapped[str | None] = mapped_column(String(120))
    region: Mapped[Region | None] = mapped_column(str_enum(Region, 30))
    city: Mapped[str | None] = mapped_column(String(120))
    website: Mapped[str | None] = mapped_column(String(300))
    logo_url: Mapped[str | None] = mapped_column(String(500))

    #: Its public page exists only once the organisation publishes it.
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Set by a WomanUP administrator after checking who it is. Only a verified
    #: organisation's people may sign practical work as verified.
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: A suspended organisation keeps its records but can do nothing, and its
    #: page and listings are not shown.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class OrganizationMember(UUIDMixin, TimestampMixin, Base):
    """A person who acts for an organisation."""

    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_organization_members_org_user"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[OrgMemberRole] = mapped_column(
        str_enum(OrgMemberRole, 20), default=OrgMemberRole.MEMBER, nullable=False
    )
    added_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class OrganizationInvitation(UUIDMixin, TimestampMixin, Base):
    """An organisation's invitation to a candidate who asked to be findable."""

    __tablename__ = "organization_invitations"
    __table_args__ = (
        Index("ix_organization_invitations_user_status", "user_id", "status"),
        Index("ix_organization_invitations_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The listing it is about, when there is one.
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="SET NULL")
    )
    message: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[InvitationStatus] = mapped_column(
        str_enum(InvitationStatus, 20), default=InvitationStatus.PENDING, nullable=False
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
