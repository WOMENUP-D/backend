"""Opportunities pulled from partner platforms, and applications to them."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import (
    ApplicationStatus,
    EventFormat,
    OpportunitySource,
    OpportunityType,
)
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum


class Opportunity(UUIDMixin, TimestampMixin, Base):
    """A vacancy, grant, investment or marketplace slot — or an event.

    `external_id` + `source` is the idempotency key for the sync job — a
    partner platform re-sending the same item updates rather than duplicates.
    """

    __tablename__ = "opportunities"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_opportunities_source"),
        Index("ix_opportunities_type_active", "type", "is_active"),
        Index("ix_opportunities_deadline", "deadline"),
        Index("ix_opportunities_starts_at", "starts_at"),
    )

    source: Mapped[OpportunitySource] = mapped_column(
        str_enum(OpportunitySource, 20), nullable=False, index=True
    )
    external_id: Mapped[str | None] = mapped_column(String(120))
    type: Mapped[OpportunityType] = mapped_column(str_enum(OpportunityType, 30), nullable=False)

    # Translatable like every other content field on the portal. A partner
    # platform sends one language; the sync stores it under that key and the
    # reader falls back uz -> ru -> en.
    title_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    description_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    organisation: Mapped[str | None] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(60), index=True)

    required_skills: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    eligibility: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    reward: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False, comment="Salary range, grant amount, etc."
    )

    #: For an event, the last moment to register; otherwise the last moment to apply.
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Where to register or apply outside WomanUP, when the organiser takes it there.
    external_url: Mapped[str | None] = mapped_column(String(500))

    # An event is a listing with a time. Everything else about it — organiser,
    # topics (the skills column), age rule, registration — is the listing's own.
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    format: Mapped[EventFormat | None] = mapped_column(str_enum(EventFormat, 10))
    #: The place, for an event she attends in person: a hall, an address.
    venue: Mapped[str | None] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: The WomanUP organisation that published it, for listings posted on the
    #: platform. Partner-feed listings keep only the `organisation` name they
    #: were sent with; nothing links them to an organisation record by guessing.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    applications: Mapped[list[Application]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan"
    )


class Application(UUIDMixin, TimestampMixin, Base):
    """A user's application, mirrored to and from the partner platform."""

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("user_id", "opportunity_id", name="uq_applications_user_id"),
        Index("ix_applications_status_submitted", "status", "submitted_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[ApplicationStatus] = mapped_column(
        str_enum(ApplicationStatus, 20),
        default=ApplicationStatus.DRAFT,
        nullable=False,
    )
    external_application_id: Mapped[str | None] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status_history: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    opportunity: Mapped[Opportunity] = relationship(back_populates="applications")


class SavedOpportunity(UUIDMixin, TimestampMixin, Base):
    """A listing she kept to come back to. Saving is not applying: nothing
    leaves the platform and no partner is told."""

    __tablename__ = "saved_opportunities"
    __table_args__ = (
        UniqueConstraint("user_id", "opportunity_id", name="uq_saved_opportunities_user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), index=True
    )


class OutcomeRecord(UUIDMixin, TimestampMixin, Base):
    """A confirmed result returned by a partner platform: hired, funded, first
    sale. This is what the North Star metric counts."""

    __tablename__ = "outcome_records"
    __table_args__ = (
        Index("ix_outcome_records_user_type", "user_id", "outcome_type"),
        Index("ix_outcome_records_created_at", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("applications.id", ondelete="SET NULL")
    )
    source: Mapped[OpportunitySource] = mapped_column(
        str_enum(OpportunitySource, 20), nullable=False
    )
    outcome_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="employment | business_registered | funding | first_sale | milestone",
    )
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
