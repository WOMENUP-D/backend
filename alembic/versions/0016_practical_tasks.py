"""Practical tasks and the attempts at them.

Additive in the strict sense: two new tables and not one change to `skills`,
`user_skills`, `skill_evidence`, `programs`, `enrollments`, `certificates`,
`learning_paths` or the diagnostic `assessments`. A passed attempt writes into
`skill_evidence` through the existing service, using evidence kinds that
already exist — no new kind, no new status, no second skills system.

`task_attempts` carries its own evaluation rather than pointing at an
evaluations table. An attempt is assessed once; a second opinion is a second
attempt, which is what a retry already is. Splitting it would buy a join and
cost the guarantee that an attempt has at most one verdict.

Nothing here stores what a task *proves*. That is `skill_evidence`'s answer and
`services.skills` is the only thing that gives it.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "practical_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title_i18n", postgresql.JSONB(), nullable=False),
        sa.Column(
            "summary_i18n",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "instructions_i18n",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "outcome_i18n",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # What the work is judged against. The same list is shown to her before
        # she starts and handed to whoever assesses it.
        sa.Column(
            "criteria", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="text"),
        sa.Column(
            "fields", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("min_chars", sa.Integer(), nullable=True),
        sa.Column("level", sa.String(length=20), nullable=True),
        sa.Column("estimated_minutes", sa.Integer(), nullable=True),
        # Labels, resolved against the taxonomy at read time — the same shape
        # as programs.skills_taught, so one alias table serves both.
        sa.Column(
            "skills_practised",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("program_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ai_reviewed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["programs.id"],
            name="fk_practical_tasks_program_id_programs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name="fk_practical_tasks_author_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_practical_tasks"),
        sa.UniqueConstraint("slug", name="uq_practical_tasks_slug"),
    )
    op.create_index("ix_practical_tasks_program_id", "practical_tasks", ["program_id"])
    op.create_index(
        "ix_practical_tasks_published_order", "practical_tasks", ["is_published", "order_index"]
    )

    op.create_table(
        "task_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # A retry is a new row. "Needs improvement, then passed" is the story of
        # her learning and overwriting the first attempt would erase the work.
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="started"),
        sa.Column(
            "submission", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        # Null until somebody has actually assessed it. False is a verdict;
        # absence is not.
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column(
            "criteria_met",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("evaluator_kind", sa.String(length=20), nullable=True),
        sa.Column("evaluator_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["practical_tasks.id"],
            name="fk_task_attempts_task_id_practical_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_task_attempts_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["evaluator_id"],
            ["users.id"],
            name="fk_task_attempts_evaluator_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_attempts"),
        sa.UniqueConstraint("task_id", "user_id", "attempt_no", name="uq_task_attempts_task_id"),
    )
    op.create_index("ix_task_attempts_task_id", "task_attempts", ["task_id"])
    op.create_index("ix_task_attempts_user_id", "task_attempts", ["user_id"])
    op.create_index("ix_task_attempts_user_status", "task_attempts", ["user_id", "status"])
    op.create_index(
        "ix_task_attempts_status_submitted", "task_attempts", ["status", "submitted_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_attempts_status_submitted", table_name="task_attempts")
    op.drop_index("ix_task_attempts_user_status", table_name="task_attempts")
    op.drop_index("ix_task_attempts_user_id", table_name="task_attempts")
    op.drop_index("ix_task_attempts_task_id", table_name="task_attempts")
    op.drop_table("task_attempts")

    op.drop_index("ix_practical_tasks_published_order", table_name="practical_tasks")
    op.drop_index("ix_practical_tasks_program_id", table_name="practical_tasks")
    op.drop_table("practical_tasks")
