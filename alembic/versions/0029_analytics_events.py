"""Product analytics: sessions and sources on page views, an event stream,
and a presence table for "online now".

Additive. `page_views` gains nullable columns (a browser session id, the
referrer host, coarse device/browser/OS families, an entry flag and a staff
flag); rows written before this revision keep NULLs, which the dashboard
reports as "collected since <date>" rather than as zeros.

`analytics_events` records business actions beside the records they describe
and the few steps only the browser can see; `dedupe_key` makes a retried
request land on a unique index instead of counting twice.

`visitor_presence` holds one row per visitor, overwritten on every view and
heartbeat, so the live count never scans the traffic table.

Nothing here stores an IP address, a user agent string, a query string or any
free text from the browser.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("page_views", sa.Column("session_id", sa.String(length=32), nullable=True))
    op.add_column(
        "page_views",
        sa.Column("is_entry", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("page_views", sa.Column("referrer_host", sa.String(length=120), nullable=True))
    op.add_column("page_views", sa.Column("device", sa.String(length=10), nullable=True))
    op.add_column("page_views", sa.Column("browser", sa.String(length=20), nullable=True))
    op.add_column("page_views", sa.Column("os", sa.String(length=20), nullable=True))
    op.add_column(
        "page_views",
        sa.Column("is_staff", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_page_views_session_occurred", "page_views", ["session_id", "occurred_at"])

    op.create_table(
        "analytics_events",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False, server_default="server"),
        sa.Column("visitor_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("session_id", sa.String(length=32), nullable=True),
        sa.Column("path", sa.String(length=120), nullable=True),
        sa.Column("locale", sa.String(length=10), nullable=True),
        sa.Column("is_staff", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("entity_type", sa.String(length=30), nullable=True),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column(
            "properties",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("dedupe_key", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_analytics_events_occurred_at", "analytics_events", ["occurred_at"])
    op.create_index(
        "ix_analytics_events_name_occurred", "analytics_events", ["name", "occurred_at"]
    )
    op.create_index(
        "ix_analytics_events_user_occurred", "analytics_events", ["user_id", "occurred_at"]
    )
    op.create_index("ix_analytics_events_entity", "analytics_events", ["entity_type", "entity_id"])
    op.create_index(
        "uq_analytics_events_dedupe_key",
        "analytics_events",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )

    op.create_table(
        "visitor_presence",
        sa.Column("visitor_hash", sa.String(length=64), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("path", sa.String(length=120), nullable=True),
        sa.Column("is_staff", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_visitor_presence_last_seen", "visitor_presence", ["last_seen_at"])


def downgrade() -> None:
    op.drop_table("visitor_presence")
    op.drop_table("analytics_events")
    op.drop_index("ix_page_views_session_occurred", table_name="page_views")
    for column in (
        "is_staff",
        "os",
        "browser",
        "device",
        "referrer_host",
        "is_entry",
        "session_id",
    ):
        op.drop_column("page_views", column)
