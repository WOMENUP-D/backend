"""Consent tracking — every data-sharing event is bound to a consent record."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import ConsentScope
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum
from app.models.user import User


class ConsentLog(UUIDMixin, TimestampMixin, Base):
    """Append-only. Withdrawing consent writes a new row with accepted=False;
    rows are never updated, so the full history stays auditable."""

    __tablename__ = "consent_logs"
    __table_args__ = (
        Index("ix_consent_logs_user_scope_created", "user_id", "scope", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[ConsentScope] = mapped_column(str_enum(ConsentScope, 40), nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(400))

    user: Mapped[User] = relationship(back_populates="consents")
