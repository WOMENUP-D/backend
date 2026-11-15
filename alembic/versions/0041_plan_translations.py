"""Cached translations of a development plan's text.

A plan is written once, in the language she was reading the site in. When she
switches the interface language the roadmap stayed in the old one — an Uzbek
page with a Russian plan. `translations` caches the plan's readable text per
language (title, summary, each step's action and description), filled on
request by `POST /plans/{id}/translate`; reads only look it up.

Additive: one JSONB column with an empty default. Downgrade drops it; the plan
itself is untouched either way.

Revision ID: 0041
Revises: 0040
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "development_plans",
        sa.Column(
            "translations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("development_plans", "translations")
