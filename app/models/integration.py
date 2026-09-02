"""Outbox for partner-platform traffic."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import IntegrationEventStatus, IntegrationSystem
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum


class IntegrationEvent(UUIDMixin, TimestampMixin, Base):
    """Transactional outbox: the API writes an event in the same transaction as
    the domain change, and a worker delivers it. Payloads carry only the fields
    the destination needs, never the whole profile."""

    __tablename__ = "integration_events"
    __table_args__ = (
        Index("ix_integration_events_status_next_retry", "status", "next_retry_at"),
        Index("ix_integration_events_system_created", "system", "created_at"),
    )

    system: Mapped[IntegrationSystem] = mapped_column(
        str_enum(IntegrationSystem, 20), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    consent_scope: Mapped[str | None] = mapped_column(
        String(40), comment="Consent scope that authorised this transfer"
    )

    status: Mapped[IntegrationEventStatus] = mapped_column(
        str_enum(IntegrationEventStatus, 20),
        default=IntegrationEventStatus.PENDING,
        nullable=False,
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
