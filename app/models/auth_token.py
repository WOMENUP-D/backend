"""Tokens the portal no longer honours.

A JWT is valid until it expires; signing out, or a refresh token being spent,
has to be remembered somewhere for that to stop being true. One row per
revoked token id (`jti`), kept until the token would have expired anyway and
then dropped. The table holds ids, never the tokens themselves.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class RevokedToken(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "revoked_tokens"

    #: The `jti` claim of the token that must not be accepted again.
    jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), index=True)
    #: `signed_out`, `rotated`, `reused` — why it was revoked, for the audit trail.
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    #: When the token would have expired on its own; the row is deleted after.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
