"""Career paths: a direction toward a kind of work, over records that already exist.

Two tables, deliberately thin:

* `career_paths` is the direction itself — a stable slug, its name and its
  promise in every language, which side of working life it sits on, how
  demanding it is, the skills the work asks for, the learning path that best
  prepares for it and the kinds of listing it leads to. Nothing about
  *content*: a career path owns no course, no task and no listing.
* `user_career_paths` is the fact that a woman chose one. One row per woman,
  because "my direction" is a single answer; choosing another moves the row.

What is deliberately **not** stored, and why:

* **Related courses, tasks and listings.** They are read off the skills every
  time — a course that teaches one of the path's skills belongs to it, and so
  does a task that practises one and an open listing that asks for one. A
  stored list would go stale the day an author published a new course, and it
  could name a listing that has since closed.
* **Her progress.** Where she stands is derived from her enrollments, task
  attempts, skills, portfolio and applications, exactly as the learning path
  derives it from enrollments. A career path cannot claim she is further along
  than the records it is read from.
* **Development Score dimensions.** Every skill already carries the dimensions
  it builds; the path's dimensions are the union of its skills'.

Skills are stored as **canonical slugs** and resolved through the skill index,
the way a portfolio project stores them — so a skill renamed or given a new
alias is still the same skill here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import CareerCategory, ProficiencyLevel
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum
from app.models.learning_path import LearningPath


class CareerPath(UUIDMixin, TimestampMixin, Base):
    """A direction toward a kind of work, and what preparing for it involves."""

    __tablename__ = "career_paths"

    slug: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    title_i18n: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: One sentence: what the work is. Read on the catalogue card.
    summary_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: One paragraph: what the path prepares her for. Never a promise of a job.
    description_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    category: Mapped[CareerCategory] = mapped_column(str_enum(CareerCategory, 20), nullable=False)
    #: How demanding the direction is to start on.
    level: Mapped[ProficiencyLevel | None] = mapped_column(str_enum(ProficiencyLevel, 20))

    #: The skills the work asks for, most important first, by canonical slug.
    skill_slugs: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list, nullable=False)
    #: The learning path that best prepares for this work, when one exists.
    learning_path_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("learning_paths.id", ondelete="SET NULL")
    )
    #: The kinds of listing the direction leads to (`OpportunityType` values).
    #: A vacancy is the end of an employment path and a grant is not, even when
    #: both happen to ask for the same skill.
    opportunity_types: Mapped[list[str]] = mapped_column(
        ARRAY(String(30)), default=list, nullable=False
    )

    #: Catalogue order. Curation, not ranking.
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    learning_path: Mapped[LearningPath | None] = relationship(lazy="selectin")


class UserCareerPath(UUIDMixin, TimestampMixin, Base):
    """The direction she chose. Holds no progress — only the choice and when."""

    __tablename__ = "user_career_paths"
    __table_args__ = (UniqueConstraint("user_id", name="uq_user_career_paths_user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    career_path_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("career_paths.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    chosen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
