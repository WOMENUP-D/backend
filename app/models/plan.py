"""AI-generated individual development plan (roadmap)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import GoalHorizon, PlanItemStatus, Priority, ScoreDimension
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum

if TYPE_CHECKING:
    from app.models.program import Program


class DevelopmentPlan(UUIDMixin, TimestampMixin, Base):
    """A roadmap proposal. Nothing takes effect until the user confirms it —
    the AI recommends, the user decides (section 06 guardrail)."""

    __tablename__ = "development_plans"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    horizon: Mapped[GoalHorizon] = mapped_column(
        str_enum(GoalHorizon, 5),
        default=GoalHorizon.M6,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # AI provenance — required for the recommendation audit trail.
    generated_by_ai: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(80))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    trace_id: Mapped[str | None] = mapped_column(String(80))
    rationale: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    items: Mapped[list[PlanItem]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="PlanItem.order_index",
    )

    @property
    def progress_percent(self) -> int:
        """Done over the steps she kept: a step she removed (skipped) is not
        one she failed to do, so it never holds the plan below 100 % (D-16)."""
        kept = [item for item in self.items if item.status != PlanItemStatus.SKIPPED]
        if not kept:
            return 0
        done = sum(1 for item in kept if item.status == PlanItemStatus.DONE)
        return round(done * 100 / len(kept))


class PlanItem(UUIDMixin, TimestampMixin, Base):
    """One concrete action in the roadmap, optionally bound to a programme."""

    __tablename__ = "plan_items"

    plan_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("development_plans.id", ondelete="CASCADE"),
        index=True,
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    action: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    dimension: Mapped[ScoreDimension | None] = mapped_column(str_enum(ScoreDimension, 40))
    priority: Mapped[Priority] = mapped_column(
        str_enum(Priority, 10),
        default=Priority.MEDIUM,
        nullable=False,
    )
    status: Mapped[PlanItemStatus] = mapped_column(
        str_enum(PlanItemStatus, 20),
        default=PlanItemStatus.PLANNED,
        nullable=False,
    )
    due_date: Mapped[date | None] = mapped_column(Date)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    program_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("programs.id", ondelete="SET NULL")
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="SET NULL")
    )

    plan: Mapped[DevelopmentPlan] = relationship(back_populates="items")
    # Loaded with the item, so a step can link straight to the course's own
    # site without a second round trip from the page.
    program: Mapped[Program | None] = relationship(lazy="selectin", viewonly=True)

    @property
    def self_report(self) -> bool:
        """Whether she marks this step done herself. A step on one of our own
        courses closes when the course is finished; everything else — a step
        with no course, a course on another site — has no other way to close
        (QA audit D-02)."""
        return self.program is None or self.program.external_url is not None

    @property
    def program_url(self) -> str | None:
        """Where the course lives when it is someone else's (Stepik, a
        university): the step opens that page rather than a catalogue card."""
        return self.program.external_url if self.program is not None else None
