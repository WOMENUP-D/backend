"""Practical tasks: applying what a course taught, and being assessed on it.

Two tables, and the restraint is deliberate.

`practical_tasks` is the catalogue entry — what to do, what to hand in, and
what it will be judged against. It names the skills it practises the way every
other content type in this codebase does: as the labels an author wrote,
resolved against the taxonomy through `SkillIndex`. A join table would be a
second way of saying what `programs.skills_taught` and
`opportunities.required_skills` already say one way.

`task_attempts` is one woman's run at one task: what she submitted, and what
came back. Evaluation lives on the attempt rather than in a table of its own
because an attempt is evaluated once — a second opinion is a second attempt,
which is exactly what a retry is.

What is deliberately **not** here: skill evidence. A passed attempt writes into
`skill_evidence` through `services.skills`, the one place that decides what a
piece of evidence is worth. Storing what a task "proves" on the task itself
would be a second skills system with a different opinion.
"""

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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import (
    EvaluatorKind,
    ProficiencyLevel,
    TaskStatus,
    TaskSubmissionKind,
)
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum


class PracticalTask(UUIDMixin, TimestampMixin, Base):
    """One piece of work to do and be assessed on."""

    __tablename__ = "practical_tasks"
    __table_args__ = (Index("ix_practical_tasks_published_order", "is_published", "order_index"),)

    slug: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    title_i18n: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: One sentence: what she will have done by the end.
    summary_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: The brief itself — what to do, step by step.
    instructions_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: What a finished piece of work looks like.
    outcome_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: What it is judged against: [{"key": "...", "text_i18n": {...}}]. The same
    #: list is shown to her before she starts and handed to the evaluator, so
    #: she is never assessed against something she was not told.
    criteria: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    kind: Mapped[TaskSubmissionKind] = mapped_column(
        str_enum(TaskSubmissionKind, 20), default=TaskSubmissionKind.TEXT, nullable=False
    )
    #: For `fields`: [{"key": "...", "label_i18n": {...}, "min_chars": 80}].
    fields: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: For `text`: the shortest answer that could plausibly meet the brief.
    #: A length check is not an assessment and is never treated as one — it
    #: only stops an empty box being filed as work.
    min_chars: Mapped[int | None] = mapped_column(Integer)

    #: How demanding the task is, in the vocabulary courses and skills share.
    #: It is also the level the evidence a pass writes is pitched at; null
    #: means the evidence claims a status but no level.
    level: Mapped[ProficiencyLevel | None] = mapped_column(str_enum(ProficiencyLevel, 20))
    estimated_minutes: Mapped[int | None] = mapped_column(Integer)

    #: The skills doing this task practises, as the author wrote them. Resolved
    #: against the taxonomy at read time, exactly like `programs.skills_taught`.
    skills_practised: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)

    #: The course this task belongs with, when it belongs with one. Nullable
    #: on purpose: a task may stand on its own, and a path reaches it through
    #: the programme rather than through a second link of its own.
    program_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("programs.id", ondelete="SET NULL"), index=True
    )

    #: Whether the platform may assess this task itself. False means every
    #: submission waits for a person.
    ai_reviewed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    attempts: Mapped[list[TaskAttempt]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class TaskAttempt(UUIDMixin, TimestampMixin, Base):
    """One woman's run at one task, and what came back.

    A retry is a new row rather than an edit: "needs improvement, then passed"
    is the story of her learning, and overwriting the first attempt would erase
    the part that was work.
    """

    __tablename__ = "task_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "user_id", "attempt_no", name="uq_task_attempts_task_id"),
        Index("ix_task_attempts_user_status", "user_id", "status"),
        Index("ix_task_attempts_evaluated_at", "evaluated_at"),
        Index("ix_task_attempts_status_submitted", "status", "submitted_at"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("practical_tasks.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        str_enum(TaskStatus, 20), default=TaskStatus.STARTED, nullable=False
    )

    #: What she handed in: {"text": ...} | {"link": ...} | {"fields": {...}}.
    #: Shaped by the task's `kind` and validated on the server before it is
    #: stored — the browser's opinion of a valid submission is not consulted.
    submission: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- the evaluation -----------------------------------------------------
    #: None until somebody has actually assessed it. `False` is a real verdict;
    #: absence is not.
    passed: Mapped[bool | None] = mapped_column(Boolean)
    score: Mapped[float | None] = mapped_column(Float)
    #: What was good and what to fix, in her language. Never generated when an
    #: evaluation did not happen.
    feedback: Mapped[str | None] = mapped_column(Text)
    #: Per-criterion verdicts: [{"key": ..., "met": bool, "note": ...}].
    criteria_met: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    evaluator_kind: Mapped[EvaluatorKind | None] = mapped_column(str_enum(EvaluatorKind, 20))
    #: The account that signed it, when a person did. This is what makes
    #: mentor and partner evidence traceable to somebody.
    evaluator_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The verified organisation a partner signed for, when a partner signed.
    #: Internal: it is never shown on a public portfolio.
    evaluator_org_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL")
    )

    task: Mapped[PracticalTask] = relationship(back_populates="attempts", lazy="selectin")
