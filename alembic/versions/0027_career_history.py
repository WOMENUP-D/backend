"""Career history: where she has worked and where she has studied.

Additive. Two new tables, one row per job and one per place of study, so the
profile can read like a CV rather than three codes. Nothing existing changes and
nothing is back-filled: the scalar profile fields stay as they are, and a woman
who never opens the new sections loses nothing.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "work_experiences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization", sa.String(length=200), nullable=False),
        sa.Column("position", sa.String(length=160), nullable=False),
        sa.Column("location", sa.String(length=120), nullable=True),
        # Month-precise: stored as the first of the month. Empty end = present.
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("client_ref", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_work_experiences_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_work_experiences"),
        sa.UniqueConstraint("user_id", "client_ref", name="uq_work_experiences_user_id"),
    )
    op.create_index("ix_work_experiences_user_id", "work_experiences", ["user_id"])

    op.create_table(
        "education_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("institution", sa.String(length=200), nullable=False),
        sa.Column("degree", sa.String(length=40), nullable=True),
        sa.Column("field_of_study", sa.String(length=160), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("client_ref", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_education_entries_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_education_entries"),
        sa.UniqueConstraint("user_id", "client_ref", name="uq_education_entries_user_id"),
    )
    op.create_index("ix_education_entries_user_id", "education_entries", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_education_entries_user_id", table_name="education_entries")
    op.drop_table("education_entries")
    op.drop_index("ix_work_experiences_user_id", table_name="work_experiences")
    op.drop_table("work_experiences")
