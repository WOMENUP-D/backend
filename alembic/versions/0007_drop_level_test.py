"""Drop the adaptive level test.

The questionnaire now ends where it ends: it asks what she wants to learn and
how ready she is, and the plan is built from that. The generated quiz that used
to follow is gone, and with it the column that stored its state.

`downgrade` puts the column back, empty — the stored runs themselves are not
recoverable, and nothing read them.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("learning_profiles", "level_test")


def downgrade() -> None:
    op.add_column(
        "learning_profiles",
        sa.Column(
            "level_test",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
