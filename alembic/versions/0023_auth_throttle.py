"""Rate-limit counters for the authentication endpoints.

Additive: one new table. The counters used to live in each worker's memory,
where they were lost on restart, not shared between workers, and — for the OTP
attempt counter — rolled back with the failed request that should have been
counted. Keys are stored as HMAC digests, so no address or IP is kept here.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_throttle",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bucket", sa.String(length=64), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_unique_constraint("uq_auth_throttle_bucket", "auth_throttle", ["bucket"])
    op.create_index("ix_auth_throttle_expires_at", "auth_throttle", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_throttle_expires_at", table_name="auth_throttle")
    op.drop_constraint("uq_auth_throttle_bucket", "auth_throttle", type_="unique")
    op.drop_table("auth_throttle")
