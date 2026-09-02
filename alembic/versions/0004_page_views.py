"""Site traffic: page views.

Nothing counted a visit before this — the dashboard could report registered and
active accounts but not how many people actually came to the site, and guests
were invisible entirely.

Stores no IP address, no user agent and no query string; the visitor is a hashed
first-party cookie id (section 08, data minimisation).

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "page_views",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("visitor_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("path", sa.String(length=120), nullable=False),
        sa.Column("locale", sa.String(length=10), nullable=True),
        sa.Column("is_authenticated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_page_views_visitor_hash", "page_views", ["visitor_hash"])
    op.create_index("ix_page_views_user_id", "page_views", ["user_id"])
    op.create_index("ix_page_views_path", "page_views", ["path"])
    op.create_index("ix_page_views_occurred", "page_views", ["occurred_at"])
    op.create_index("ix_page_views_visitor_occurred", "page_views", ["visitor_hash", "occurred_at"])


def downgrade() -> None:
    op.drop_table("page_views")
