"""Events: a listing with a time and a place.

Additive. Four nullable columns on `opportunities` and one index; every
existing row keeps its meaning — a listing without `starts_at` is not an event:

* `starts_at`, `ends_at` — when it happens.
* `format` — `online` or `offline`.
* `venue` — where, for an event attended in person.

The new event kinds (workshop, seminar, conference, forum, networking) and the
`event_reminder` notification trigger are values of varchar enum columns and
need no schema change. No event is created here: events are published by
organisations or sent by partner feeds.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("opportunities", sa.Column("starts_at", sa.DateTime(timezone=True)))
    op.add_column("opportunities", sa.Column("ends_at", sa.DateTime(timezone=True)))
    op.add_column("opportunities", sa.Column("format", sa.String(length=10)))
    op.add_column("opportunities", sa.Column("venue", sa.String(length=300)))
    op.create_index("ix_opportunities_starts_at", "opportunities", ["starts_at"])


def downgrade() -> None:
    op.drop_index("ix_opportunities_starts_at", table_name="opportunities")
    op.drop_column("opportunities", "venue")
    op.drop_column("opportunities", "format")
    op.drop_column("opportunities", "ends_at")
    op.drop_column("opportunities", "starts_at")
