"""Her working and study history, one row per place.

The profile's scalar fields — `education_level`, `employment_status`,
`profession` — say where she is now, in the platform's own codes. What they
cannot say is the path: that she taught at School No. 45 for six years and then
kept the books for a shop, or that she finished college before the university.
That is what a CV is, and it is what these two tables hold.

Dates are month-precise, stored as the first of the month: nobody remembers the
day she started a job, and asking for it is how a form gets abandoned. An empty
`end_date` means "to the present".

Personal data. It is shown to her, and to nobody else yet: it is not part of
`minimal_profile_payload`, and not a section of the public portfolio.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class WorkExperience(UUIDMixin, TimestampMixin, Base):
    """One job, or one stretch of her own business."""

    __tablename__ = "work_experiences"
    __table_args__ = (
        # A retried "save" is the same entry — see `PortfolioProject`.
        UniqueConstraint("user_id", "client_ref", name="uq_work_experiences_user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    #: Where: "45-maktab", "«Anor» doʻkoni", or "Oʻz biznesim".
    organization: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[str] = mapped_column(String(160), nullable=False)
    location: Mapped[str | None] = mapped_column(String(120))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)
    client_ref: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))


class EducationEntry(UUIDMixin, TimestampMixin, Base):
    """One school, college, university or long course."""

    __tablename__ = "education_entries"
    __table_args__ = (
        UniqueConstraint("user_id", "client_ref", name="uq_education_entries_user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    institution: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The questionnaire's level codes where one fits (`school`, `college`,
    #: `bachelor`, `master`), so it is translated where it is shown.
    degree: Mapped[str | None] = mapped_column(String(40))
    field_of_study: Mapped[str | None] = mapped_column(String(160))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)
    client_ref: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
