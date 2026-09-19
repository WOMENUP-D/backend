"""Learning programmes, modules, lessons and enrollments."""

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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import (
    EnrollmentStatus,
    Language,
    LessonKind,
    ProficiencyLevel,
    ProgramCategory,
    ProgramFormat,
)
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum


class Program(UUIDMixin, TimestampMixin, Base):
    """A course. Field set follows the 'dastur kartochkasi' standard, section 04A."""

    __tablename__ = "programs"
    __table_args__ = (Index("ix_programs_category_published", "category", "is_published"),)

    slug: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    title_i18n: Mapped[dict] = mapped_column(JSONB, nullable=False)
    goal_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    description_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    category: Mapped[ProgramCategory] = mapped_column(
        str_enum(ProgramCategory, 40), nullable=False, index=True
    )
    format: Mapped[ProgramFormat] = mapped_column(
        str_enum(ProgramFormat, 20),
        default=ProgramFormat.VIDEO,
        nullable=False,
    )
    language: Mapped[Language] = mapped_column(
        str_enum(Language, 5),
        default=Language.UZ,
        nullable=False,
    )
    #: How demanding the course is, in the vocabulary skills are measured in.
    #: Nullable: a course nobody has classified says nothing rather than
    #: claiming to be for beginners.
    level: Mapped[ProficiencyLevel | None] = mapped_column(str_enum(ProficiencyLevel, 20))

    # Target segment: age range, status, region, need.
    target_age_min: Mapped[int | None] = mapped_column(Integer)
    target_age_max: Mapped[int | None] = mapped_column(Integer)
    target_regions: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    target_segments: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)

    duration_hours: Mapped[float | None] = mapped_column(Float)
    duration_weeks: Mapped[int | None] = mapped_column(Integer)
    prerequisites: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    # 3-7 measurable outcomes, enforced by the schema layer.
    learning_outcomes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    skills_taught: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)

    assessment_type: Mapped[str | None] = mapped_column(String(40))
    has_certificate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Where the learner goes next: edu_job | invest_hub | commerce | program:<slug>
    next_step: Mapped[str | None] = mapped_column(String(120))

    provider: Mapped[str | None] = mapped_column(String(200))
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: The education provider that offers it, when a member organisation does.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )
    cover_url: Mapped[str | None] = mapped_column(String(500))

    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    modules: Mapped[list[ProgramModule]] = relationship(
        back_populates="program",
        cascade="all, delete-orphan",
        order_by="ProgramModule.order_index",
    )


class ProgramModule(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "program_modules"

    program_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("programs.id", ondelete="CASCADE"), index=True
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    title_i18n: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    media_url: Mapped[str | None] = mapped_column(String(500))
    duration_minutes: Mapped[int | None] = mapped_column(Integer)

    program: Mapped[Program] = relationship(back_populates="modules")
    # Loaded with the module: the course page draws the whole contents at once,
    # and a lazy load per module would fire a query inside the response.
    lessons: Mapped[list[ProgramLesson]] = relationship(
        back_populates="module",
        cascade="all, delete-orphan",
        order_by="ProgramLesson.order_index",
        lazy="selectin",
    )


class ProgramLesson(UUIDMixin, TimestampMixin, Base):
    """One lesson inside a module — what a woman actually opens and reads.

    A module may have none, and that is not a gap: the whole seeded catalogue
    worked at module granularity before lessons existed, and progress still
    counts modules for those. Where lessons exist they become the unit of
    progress, because they are what she completes.
    """

    __tablename__ = "program_lessons"
    __table_args__ = (UniqueConstraint("module_id", "slug", name="uq_program_lessons_module_id"),)

    module_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("program_modules.id", ondelete="CASCADE"), index=True
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: What the URL carries, so a lesson keeps its link when a module is reordered.
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    title_i18n: Mapped[dict] = mapped_column(JSONB, nullable=False)
    kind: Mapped[LessonKind] = mapped_column(
        str_enum(LessonKind, 20), default=LessonKind.READING, nullable=False
    )
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    #: The body, as the blocks the reader renders: paragraphs, headings, lists,
    #: code, callouts. A lesson is read whole and never queried by paragraph.
    blocks: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: Links that leave the platform: a template, a PDF, a repository.
    resources: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    media_url: Mapped[str | None] = mapped_column(String(500))

    module: Mapped[ProgramModule] = relationship(back_populates="lessons")


class Enrollment(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("user_id", "program_id", name="uq_enrollments_user_id"),
        Index("ix_enrollments_status_started", "status", "started_at"),
        Index("ix_enrollments_completed_at", "completed_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("programs.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[EnrollmentStatus] = mapped_column(
        str_enum(EnrollmentStatus, 20),
        default=EnrollmentStatus.ENROLLED,
        nullable=False,
    )
    progress_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_modules: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: The same idea one level down. Which of the two counts towards progress is
    #: decided by what the programme has — see `services.learning`.
    completed_lessons: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    final_score: Mapped[float | None] = mapped_column(Float)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    certificate: Mapped[Certificate | None] = relationship(
        back_populates="enrollment", cascade="all, delete-orphan", uselist=False
    )


class Certificate(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "certificates"
    __table_args__ = (Index("ix_certificates_issued_at", "issued_at"),)

    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("enrollments.id", ondelete="CASCADE"),
        unique=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    serial_number: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    file_url: Mapped[str | None] = mapped_column(String(500))
    verification_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    enrollment: Mapped[Enrollment] = relationship(back_populates="certificate")
