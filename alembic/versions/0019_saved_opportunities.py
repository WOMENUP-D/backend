"""Saved listings: a listing she kept to come back to.

Additive: one new table, no change to `opportunities` or `applications`. Saving
is deliberately not applying — no partner is told, nothing leaves the platform,
and the application flow is unchanged by it.

What this migration does **not** add, and why: no remote/on-site flag and no
experience level. Neither exists in the partner feed, and a filter over a
column nobody fills would return nothing while looking like it works.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_opportunities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opportunity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_saved_opportunities_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["opportunities.id"],
            name="fk_saved_opportunities_opportunity_id_opportunities",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_saved_opportunities"),
        sa.UniqueConstraint("user_id", "opportunity_id", name="uq_saved_opportunities_user_id"),
    )
    op.create_index("ix_saved_opportunities_user_id", "saved_opportunities", ["user_id"])
    op.create_index(
        "ix_saved_opportunities_opportunity_id", "saved_opportunities", ["opportunity_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_saved_opportunities_opportunity_id", table_name="saved_opportunities")
    op.drop_index("ix_saved_opportunities_user_id", table_name="saved_opportunities")
    op.drop_table("saved_opportunities")
