"""Lessons inside a module, and the level a programme teaches at.

A programme had modules and nothing under them, so the learning section could
only ever say "module 3 of 7" — while the screens it was designed for read a
lesson at a time. This adds the level the hierarchy was missing:

    programme -> module -> lesson

and leaves everything above it untouched. `enrollments.completed_modules` keeps
working exactly as before: a module with no lessons completes the way it always
did, which is what the whole seeded catalogue does until lessons are written for
it. `completed_lessons` is the same idea one level down, and progress is
recomputed from whichever of the two the programme actually has.

`programs.level` is nullable on purpose. It says how demanding a course is, and
a course nobody has classified should say nothing rather than guess — the skill
evidence a completion writes falls back to its own floor when the level is not
set.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("programs", sa.Column("level", sa.String(length=20), nullable=True))
    op.add_column(
        "enrollments",
        sa.Column(
            "completed_lessons",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    op.create_table(
        "program_lessons",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("module_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        # Addressed by slug in the URL, so a lesson keeps its link when the
        # order of a module changes.
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title_i18n", postgresql.JSONB(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="reading"),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        # The lesson body, as the blocks the reader renders: paragraphs,
        # headings, lists, code, callouts. JSONB rather than a table per block
        # type — a lesson is read whole, never queried by paragraph.
        sa.Column(
            "blocks", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "resources", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("media_url", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["module_id"],
            ["program_modules.id"],
            name="fk_program_lessons_module_id_program_modules",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_program_lessons"),
        sa.UniqueConstraint("module_id", "slug", name="uq_program_lessons_module_id"),
    )
    op.create_index("ix_program_lessons_module_id", "program_lessons", ["module_id"])


def downgrade() -> None:
    op.drop_index("ix_program_lessons_module_id", table_name="program_lessons")
    op.drop_table("program_lessons")
    op.drop_column("enrollments", "completed_lessons")
    op.drop_column("programs", "level")
