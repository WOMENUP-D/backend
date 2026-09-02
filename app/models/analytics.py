"""Site traffic.

Section 09 asks the dashboard for registered and active users, but nothing on
the platform counted a *visit* — a woman who browsed the catalogue and left was
invisible, and so was every guest the AI assistant answered.

Two constraints shape the table. Data minimisation (section 08): no IP address,
no user agent, no query string is stored. And the whole point is to count
people, not requests, so a visitor carries a random first-party id that is kept
hashed — the raw cookie value never reaches the database, and the rows cannot be
tied back to a person by anyone reading them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class PageView(UUIDMixin, TimestampMixin, Base):
    """One screen opened by one visitor."""

    __tablename__ = "page_views"
    __table_args__ = (
        Index("ix_page_views_occurred", "occurred_at"),
        Index("ix_page_views_visitor_occurred", "visitor_hash", "occurred_at"),
    )

    # HMAC of the visitor's cookie id. Enough to count unique people over a
    # window; useless for identifying one.
    visitor_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Set once she signs in, so the dashboard can separate guests from members.
    # Nullable by design: most traffic is anonymous and must still be counted.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # Route only — "/dasturlar", never "/dasturlar?search=her+name".
    path: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    locale: Mapped[str | None] = mapped_column(String(10))
    is_authenticated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
