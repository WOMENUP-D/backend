"""Counters behind the authentication rate limits.

One row per key per window. The key itself — an address or an IP — is never
stored: the row is found by an HMAC digest of it, so the table carries no
personal data even though it counts people's attempts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class AuthThrottle(UUIDMixin, TimestampMixin, Base):
    """Hits for one key inside one fixed window."""

    __tablename__ = "auth_throttle"

    #: HMAC of "scope:key:window start" — see `services.rate_limit`.
    bucket: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: When the window ends. Rows older than this are deleted on the way past.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
