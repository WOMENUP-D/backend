"""Provenance and deduplication for AI-ingested news.

The news feed gained an unattended producer: a job that searches for articles
every eight hours, drafts a post from the page it fetched, and — when the
publication gate in `services.news_ingest` lets it — publishes the result.

An unattended job needs a key it cannot lose. A slug is derived from a title
and titles are rewritten between outlets and reused on anniversaries; a URL is
the one thing about an article that stays the same. `source_url_hash` is the
SHA-256 of the canonical form of `source_url` (host lowercased, `www.` and the
query string dropped, trailing slash trimmed), so the unique index below is
fixed-width and normalisation is forced through one function. With it the job
is safe to run three times a day indefinitely, and two workers racing past the
same SELECT end in an IntegrityError rather than a duplicate card in the feed.

Nullable, because every post written by a human — the seeded feed included —
has no source URL at all, and Postgres permits many nulls in a unique index.
No backfill is required: existing rows stay null and are never matched by the
deduplication query.

The `ai_interactions.feature` comment gains `news_ingest`, which is the
vocabulary that column documents. A comment, not a constraint.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_FEATURE_COMMENT_OLD = "navigator | roadmap | recommendation | content_assistant | risk_flag"
_FEATURE_COMMENT_NEW = _FEATURE_COMMENT_OLD + " | news_ingest"


def upgrade() -> None:
    op.add_column("news_posts", sa.Column("source_url_hash", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_news_posts_source_url_hash",
        "news_posts",
        ["source_url_hash"],
        unique=True,
    )
    op.alter_column(
        "ai_interactions",
        "feature",
        existing_type=sa.String(length=40),
        existing_nullable=False,
        comment=_FEATURE_COMMENT_NEW,
        existing_comment=_FEATURE_COMMENT_OLD,
    )


def downgrade() -> None:
    op.alter_column(
        "ai_interactions",
        "feature",
        existing_type=sa.String(length=40),
        existing_nullable=False,
        comment=_FEATURE_COMMENT_OLD,
        existing_comment=_FEATURE_COMMENT_NEW,
    )
    op.drop_index("ix_news_posts_source_url_hash", table_name="news_posts")
    op.drop_column("news_posts", "source_url_hash")
