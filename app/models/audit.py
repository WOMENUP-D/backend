"""Audit log and AI recommendation trace — immutable security events."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import DataClassification, RiskFlagType
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum


class AuditLog(UUIDMixin, TimestampMixin, Base):
    """Who did what to which entity, when, from where.

    Insert-only by contract: there is no update path in the service layer, and
    production grants should withhold UPDATE/DELETE on this table.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_actor_created", "actor_id", "created_at"),
    )

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_role: Mapped[str | None] = mapped_column(String(30))
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(60))
    classification: Mapped[DataClassification] = mapped_column(
        str_enum(DataClassification, 20),
        default=DataClassification.INTERNAL,
        nullable=False,
    )
    changes: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(400))
    request_id: Mapped[str | None] = mapped_column(String(60))


class AiInteraction(UUIDMixin, TimestampMixin, Base):
    """One AI call: prompt/model version, retrieved sources, confidence.

    Section 06 requires model version, prompt version and a response trace to
    be retained for every recommendation.
    """

    __tablename__ = "ai_interactions"
    __table_args__ = (Index("ix_ai_interactions_user_created", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    feature: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="navigator | roadmap | recommendation | content_assistant | risk_flag",
    )
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)

    # Sanitised — PII is scrubbed before persisting.
    prompt_excerpt: Mapped[str | None] = mapped_column(Text)
    response_excerpt: Mapped[str | None] = mapped_column(Text)
    retrieved_sources: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    confidence: Mapped[float | None] = mapped_column(Float)
    refused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column()
    input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()


class RiskFlag(UUIDMixin, TimestampMixin, Base):
    """A signal raised for a coordinator. Never an automatic sanction."""

    __tablename__ = "risk_flags"
    __table_args__ = (Index("ix_risk_flags_user_resolved", "user_id", "resolved_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    flag_type: Mapped[RiskFlagType] = mapped_column(str_enum(RiskFlagType, 30), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), default="medium", nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    notified_coordinator_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)
