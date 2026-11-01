"""Sign out everywhere, exactly.

Every token carries the session generation of its account at the moment it
was issued (`gen`). "Sign out everywhere" and a replayed refresh token bump
the account's generation, and every token from an earlier generation is
refused on its next request — on every device, with no clock comparison and
no per-token bookkeeping. Existing tokens carry no generation and read as 0,
which is also every account's starting value, so nothing is signed out by the
migration itself.

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("session_generation", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "session_generation")
