"""A photograph on a news post.

The feed drew every cover from a palette tint and a glyph — deliberately, so
that no post is ever blank and nothing is fetched from a third party while a
reader scrolls. That still holds as the fallback, but a news feed with no way
to carry a photograph is a news feed missing the thing readers came for.

The URL is expected to point at our own storage. Hotlinking a news site's image
would put a request to that site in every reader's browser, with no consent
behind it, and would break the day they move the file.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("news_posts", sa.Column("cover_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("news_posts", "cover_url")
