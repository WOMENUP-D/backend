"""User-created portfolio projects.

The only thing in the portfolio that is *stored* for its own sake. Everything
else on that page — certificates, achievements, skills, practical work — is a
reading of records other services already own, and storing a second copy of any
of them would be a second opinion waiting to disagree with the first.

A project is her own account of work she did. It names the skills it used by
their **canonical slug**, validated against the `skills` table when it is
written: an unknown label is refused rather than minted, because a project form
is the one place anybody at all could otherwise add words to the taxonomy.

A project is evidence, not verification. Saving one records *self-reported*
evidence for the skills it names — the weakest claim, and the honest one for
work nobody on the platform has looked at.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class PortfolioProject(UUIDMixin, TimestampMixin, Base):
    """One piece of work she wants to show."""

    __tablename__ = "portfolio_projects"
    __table_args__ = (
        # A retried "save" is the same project. The browser generates this once
        # per form it opens, so a double tap or a flaky connection cannot leave
        # her with two copies of the same work.
        UniqueConstraint("user_id", "client_ref", name="uq_portfolio_projects_user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    #: One line, for the card.
    summary: Mapped[str | None] = mapped_column(String(280))
    description: Mapped[str | None] = mapped_column(Text)
    #: Canonical skill slugs, each checked against `skills` when written.
    skill_slugs: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)

    project_url: Mapped[str | None] = mapped_column(String(500))
    repo_url: Mapped[str | None] = mapped_column(String(500))
    demo_url: Mapped[str | None] = mapped_column(String(500))
    #: When she finished it. Null means ongoing — and an ongoing project is not
    #: yet an achievement.
    completed_on: Mapped[date | None] = mapped_column(Date)

    #: Private until she says otherwise, and only ever shown when the portfolio
    #: itself is public too.
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    client_ref: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
