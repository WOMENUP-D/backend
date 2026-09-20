"""Diagnostics: versioned question sets, answers and the Development Score."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import ScoreDimension
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum


class AssessmentQuestion(UUIDMixin, TimestampMixin, Base):
    """One diagnostic question. Question sets are versioned so historical
    scores stay reproducible when the instrument changes."""

    __tablename__ = "assessment_questions"
    __table_args__ = (Index("ix_assessment_questions_version_dimension", "version", "dimension"),)

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    dimension: Mapped[ScoreDimension] = mapped_column(str_enum(ScoreDimension, 40), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    question_type: Mapped[str] = mapped_column(String(30), default="single_choice")
    # Localised text: {"uz": "...", "ru": "...", "en": "..."}
    text_i18n: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # [{"value": 1, "label_i18n": {...}, "weight": 0.25}, ...]
    options: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Assessment(UUIDMixin, TimestampMixin, Base):
    """One completed (or in-progress) diagnostic run."""

    __tablename__ = "assessments"
    __table_args__ = (
        Index("ix_assessments_user_completed", "user_id", "completed_at"),
        Index("ix_assessments_completed_at", "completed_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    answers: Mapped[list[AssessmentAnswer]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan", lazy="selectin"
    )
    scores: Mapped[list[DevelopmentScore]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan", lazy="selectin"
    )


class AssessmentAnswer(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "assessment_answers"
    __table_args__ = (
        UniqueConstraint(
            "assessment_id", "question_id", name="uq_assessment_answers_assessment_id"
        ),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("assessments.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("assessment_questions.id", ondelete="RESTRICT")
    )
    value: Mapped[float] = mapped_column(Float, nullable=False)
    raw_answer: Mapped[str | None] = mapped_column(Text)

    assessment: Mapped[Assessment] = relationship(back_populates="answers")


class DevelopmentScore(UUIDMixin, TimestampMixin, Base):
    """Per-dimension score with baseline / current / target, section 03."""

    __tablename__ = "development_scores"
    __table_args__ = (Index("ix_development_scores_user_dimension", "user_id", "dimension"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("assessments.id", ondelete="SET NULL")
    )
    dimension: Mapped[ScoreDimension] = mapped_column(str_enum(ScoreDimension, 40), nullable=False)
    baseline: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    current: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    target: Mapped[float | None] = mapped_column(Float)
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    assessment: Mapped[Assessment | None] = relationship(back_populates="scores")

    @property
    def progress(self) -> float:
        """Fraction of the baseline-to-target distance already covered."""
        if self.target is None or self.target <= self.baseline:
            return 0.0
        return min(max((self.current - self.baseline) / (self.target - self.baseline), 0.0), 1.0)
