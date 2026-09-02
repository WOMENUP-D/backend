"""The learning-profile questionnaire and its level test.

Distinct from the Development Score: that instrument measures eight dimensions
of life and drives the plan and the national KPIs, while this one records what
a woman wants to learn and how ready she is, and drives what the assistant
recommends.

Answers live in JSONB keyed by the question ids in `services.questionnaire`,
so rewording a question does not need a migration.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("answers", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("derived", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("level_test", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_learning_profiles_user"),
    )
    op.create_index("ix_learning_profiles_user_id", "learning_profiles", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_learning_profiles_user_id", table_name="learning_profiles")
    op.drop_table("learning_profiles")
