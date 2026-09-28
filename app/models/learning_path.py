"""Learning paths: an ordered route through courses that already exist.

Two tables and one join, deliberately thin:

* `learning_paths` is the route itself — a stable slug, its name and its
  promise in every language, how demanding it is and which Development Score
  dimension it builds. Nothing about *content*: a path owns no lessons, no
  modules and no text a course does not already hold.
* `learning_path_items` orders existing programmes inside a path. It stores a
  reference and a position, never a copy, so a course edited once is edited
  everywhere it appears and the same course may sit in several paths.
* `user_learning_paths` is the fact that a woman started one.

What is deliberately **not** stored: progress, target skills and duration.
All three are derived — progress from her enrollments, skills from the skills
the member courses teach, duration from their hours. A stored copy of any of
them is a second opinion waiting to disagree with the first, and the first is
the one the learning section already computes.

`completed_at` is the exception, and for the same reason a course has one: it
is a date, not a derivation, and it guards the things finishing a path does
once — the skill evidence it writes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import ProficiencyLevel, ScoreDimension
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum
from app.models.program import Program


class LearningPath(UUIDMixin, TimestampMixin, Base):
    """A curated route through the catalogue, toward one goal."""

    __tablename__ = "learning_paths"
    __table_args__ = (Index("ix_learning_paths_dimension_published", "dimension", "is_published"),)

    slug: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    title_i18n: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: What finishing it is meant to leave her able to do. One paragraph.
    description_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    #: The Development Score dimension the path builds, which is what lets the
    #: cabinet answer "improve this area through this path" from her own score.
    #: Nullable: a path may be about a craft rather than about a dimension.
    dimension: Mapped[ScoreDimension | None] = mapped_column(str_enum(ScoreDimension, 40))
    #: How demanding the route is as a whole, in the vocabulary courses and
    #: skills already share. It is also the level the skill evidence a finished
    #: path writes is pitched at.
    level: Mapped[ProficiencyLevel | None] = mapped_column(str_enum(ProficiencyLevel, 20))

    #: Catalogue order. Curation, not ranking: the score decides what is
    #: recommended, this decides what a visitor reads first.
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list[LearningPathItem]] = relationship(
        back_populates="path",
        cascade="all, delete-orphan",
        order_by="LearningPathItem.order_index",
        lazy="selectin",
    )


class LearningPathItem(UUIDMixin, TimestampMixin, Base):
    """One existing programme, at one position in a path.

    `is_required` carries the prerequisite rule: a step is open once every
    *required* step before it is finished. Optional steps never block the ones
    after them — they widen the route rather than lengthen it.
    """

    __tablename__ = "learning_path_items"
    __table_args__ = (
        UniqueConstraint("path_id", "program_id", name="uq_learning_path_items_path_id"),
        Index("ix_learning_path_items_path_order", "path_id", "order_index"),
    )

    path_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("learning_paths.id", ondelete="CASCADE"), index=True
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("programs.id", ondelete="CASCADE"), index=True
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    path: Mapped[LearningPath] = relationship(back_populates="items")
    # Carried with the item: every screen that lists a path names its courses,
    # and fetching them one at a time is a query per row.
    program: Mapped[Program] = relationship(lazy="selectin")


class UserLearningPath(UUIDMixin, TimestampMixin, Base):
    """That she started a path, and when it came together.

    Holds no progress. Where she stands is read from her enrollments every
    time, so a path can never claim she is further along than the courses it
    is made of.
    """

    __tablename__ = "user_learning_paths"
    __table_args__ = (
        UniqueConstraint("user_id", "path_id", name="uq_user_learning_paths_user_id"),
        Index("ix_user_learning_paths_completed_at", "completed_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    path_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("learning_paths.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Stamped once, when every required course is finished. The guard for the
    #: skill evidence finishing a path writes.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
