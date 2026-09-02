"""Mentor profiles and mentoring sessions."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin


class MentorProfile(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "mentor_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    headline: Mapped[str | None] = mapped_column(String(255))
    bio: Mapped[str | None] = mapped_column(Text)
    expertise: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    languages: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    years_of_experience: Mapped[int | None] = mapped_column(Integer)
    availability: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    max_mentees: Mapped[int] = mapped_column(Integer, default=5, nullable=False)

    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rating_avg: Mapped[float | None] = mapped_column(Float)
    sessions_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    sessions: Mapped[list[MentorSession]] = relationship(back_populates="mentor")


class MentorSession(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "mentor_sessions"

    mentor_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("mentor_profiles.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="requested", nullable=False)
    topic: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int | None] = mapped_column(Integer)
    feedback: Mapped[str | None] = mapped_column(Text)

    mentor: Mapped[MentorProfile] = relationship(back_populates="sessions")
