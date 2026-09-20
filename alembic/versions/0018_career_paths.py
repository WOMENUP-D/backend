"""Career paths over the skills, courses, tasks and listings that already exist.

Additive in the strict sense: two new tables and not one change to any
existing one. A career path references a learning path and names skills by
their canonical slug; it copies no course, no task and no listing, so dropping
both tables leaves every earlier step exactly as it was.

No progress column anywhere. Where she stands on a career path is read from
her enrollments, task attempts, skills, portfolio and applications every time
— see `services.career_path`.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "career_paths",
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
            "description_i18n",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=True),
        # Canonical skill slugs, most important first. Related courses, tasks and
        # listings are read off these rather than stored.
        sa.Column(
            "skill_slugs",
            postgresql.ARRAY(sa.String(length=80)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("learning_path_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "opportunity_types",
            postgresql.ARRAY(sa.String(length=30)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["learning_path_id"],
            ["learning_paths.id"],
            name="fk_career_paths_learning_path_id_learning_paths",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_career_paths"),
        sa.UniqueConstraint("slug", name="uq_career_paths_slug"),
    )

    op.create_table(
        "user_career_paths",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("career_path_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chosen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_career_paths_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["career_path_id"],
            ["career_paths.id"],
            name="fk_user_career_paths_career_path_id_career_paths",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_career_paths"),
        # One direction at a time: "my direction" is a single answer.
        sa.UniqueConstraint("user_id", name="uq_user_career_paths_user_id"),
    )
    op.create_index("ix_user_career_paths_career_path_id", "user_career_paths", ["career_path_id"])


def downgrade() -> None:
    op.drop_index("ix_user_career_paths_career_path_id", table_name="user_career_paths")
    op.drop_table("user_career_paths")
    op.drop_table("career_paths")
