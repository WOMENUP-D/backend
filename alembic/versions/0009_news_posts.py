"""News and announcements feed.

The portal had a catalogue, a plan and an assistant, but nothing to open on:
after registration a woman landed straight in her own empty cabinet. This table
holds the feed that now greets her — medicine, health, women in science,
and the announcements with a deadline on them.

`is_adult_only` is the age boundary in the schema: the feed withholds those
rows from a minor and from a reader whose age is not known.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "news_posts",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("title_i18n", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary_i18n", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("body_i18n", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cover_tone", sa.String(length=20), nullable=False, server_default="plum"),
        sa.Column("cover_emblem", sa.String(length=8), nullable=False, server_default="✦"),
        sa.Column("source_name", sa.String(length=160), nullable=True),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("region", sa.String(length=60), nullable=True),
        sa.Column("reading_minutes", sa.Integer(), nullable=True),
        sa.Column("is_adult_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "author_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_news_posts_slug", "news_posts", ["slug"], unique=True)
    op.create_index("ix_news_posts_category", "news_posts", ["category"])
    op.create_index("ix_news_posts_region", "news_posts", ["region"])
    op.create_index("ix_news_posts_published", "news_posts", ["is_published", "published_at"])
    op.create_index("ix_news_posts_category_published", "news_posts", ["category", "is_published"])


def downgrade() -> None:
    op.drop_table("news_posts")
