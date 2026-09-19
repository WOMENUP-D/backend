"""Learning paths over the courses that already exist.

Additive in the strict sense: three new tables and not one change to
`programs`, `program_modules`, `program_lessons`, `enrollments` or
`certificates`. A path references courses; it never copies them, and nothing
about an existing course, enrollment or certificate is touched by this
migration. Dropping every table here would leave the learning section exactly
as it was before it ran.

No progress column anywhere. How far along a path is, is read from her
enrollments — see `services.learning_path`. The only date stored is
`user_learning_paths.completed_at`, which is a fact rather than a derivation
and guards the skill evidence a finished path writes.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_paths",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title_i18n", postgresql.JSONB(), nullable=False),
        sa.Column(
            "description_i18n",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # The Development Score dimension the path builds, so a weak dimension
        # can point at a route rather than at a single course.
        sa.Column("dimension", sa.String(length=40), nullable=True),
        sa.Column("level", sa.String(length=20), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_learning_paths"),
        sa.UniqueConstraint("slug", name="uq_learning_paths_slug"),
    )
    op.create_index(
        "ix_learning_paths_dimension_published",
        "learning_paths",
        ["dimension", "is_published"],
    )

    op.create_table(
        "learning_path_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("path_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("program_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        # A step is open once every *required* step before it is finished.
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["path_id"],
            ["learning_paths.id"],
            name="fk_learning_path_items_path_id_learning_paths",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["programs.id"],
            name="fk_learning_path_items_program_id_programs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_learning_path_items"),
        sa.UniqueConstraint("path_id", "program_id", name="uq_learning_path_items_path_id"),
    )
    op.create_index("ix_learning_path_items_path_id", "learning_path_items", ["path_id"])
    op.create_index("ix_learning_path_items_program_id", "learning_path_items", ["program_id"])
    op.create_index(
        "ix_learning_path_items_path_order", "learning_path_items", ["path_id", "order_index"]
    )

    op.create_table(
        "user_learning_paths",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("path_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_learning_paths_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["path_id"],
            ["learning_paths.id"],
            name="fk_user_learning_paths_path_id_learning_paths",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_learning_paths"),
        sa.UniqueConstraint("user_id", "path_id", name="uq_user_learning_paths_user_id"),
    )
    op.create_index("ix_user_learning_paths_user_id", "user_learning_paths", ["user_id"])
    op.create_index("ix_user_learning_paths_path_id", "user_learning_paths", ["path_id"])


def downgrade() -> None:
    op.drop_index("ix_user_learning_paths_path_id", table_name="user_learning_paths")
    op.drop_index("ix_user_learning_paths_user_id", table_name="user_learning_paths")
    op.drop_table("user_learning_paths")

    op.drop_index("ix_learning_path_items_path_order", table_name="learning_path_items")
    op.drop_index("ix_learning_path_items_program_id", table_name="learning_path_items")
    op.drop_index("ix_learning_path_items_path_id", table_name="learning_path_items")
    op.drop_table("learning_path_items")

    op.drop_index("ix_learning_paths_dimension_published", table_name="learning_paths")
    op.drop_table("learning_paths")
