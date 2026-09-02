"""Notifications and their delivery attempts."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import NotificationChannel, NotificationTrigger
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum


class Notification(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_read", "user_id", "read_at"),
        Index("ix_notifications_scheduled", "scheduled_for", "sent_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        str_enum(NotificationChannel, 20),
        default=NotificationChannel.IN_APP,
        nullable=False,
    )
    trigger: Mapped[NotificationTrigger | None] = mapped_column(str_enum(NotificationTrigger, 40))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    action_url: Mapped[str | None] = mapped_column(String(500))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class NotificationPreference(UUIDMixin, TimestampMixin, Base):
    """Per-channel opt-in. No dark patterns: everything defaults to explicit."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    in_app_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    quiet_hours_start: Mapped[int | None] = mapped_column(Integer)
    quiet_hours_end: Mapped[int | None] = mapped_column(Integer)
