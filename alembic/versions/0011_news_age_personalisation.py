"""Age-based personalisation of the news feed.

The feed already knew whether a reader was old enough to be shown adult
reproductive content — a safety gate with two positions. It did not know that a
fifteen-year-old and a fifty-five-year-old, both allowed to read an article
about osteoporosis, came for entirely different things.

These columns answer that. `age_relevance` scores a post 0-100 for each of the
six brackets, `topics` records which women's-health subjects it covers, and the
three score columns carry the editorial judgements the ranker weighs beside
them. On the reader's side, `news_interests` is the controlled vocabulary she
picks on the news-preferences screen.

Nothing here withholds a post. The feed, the categories and the search read
none of these columns; only the personalised "For you" section does, and it
orders rather than filters.

Existing rows get the empty defaults and are scored on the fly by the
keyword rules in `services.news_age`, so no backfill is required for the
feature to work — running the AI editor over the archive only sharpens it.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "news_posts",
        sa.Column(
            "age_relevance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "news_posts",
        sa.Column(
            "topics",
            postgresql.ARRAY(sa.String()),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
    )
    op.add_column("news_posts", sa.Column("relevance_score", sa.Integer(), nullable=True))
    op.add_column("news_posts", sa.Column("credibility_score", sa.Integer(), nullable=True))
    op.add_column("news_posts", sa.Column("impact_score", sa.Integer(), nullable=True))
    op.add_column(
        "news_posts",
        sa.Column(
            "ai_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    op.add_column(
        "profiles",
        sa.Column(
            "news_interests",
            postgresql.ARRAY(sa.String()),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("profiles", "news_interests")
    op.drop_column("news_posts", "ai_meta")
    op.drop_column("news_posts", "impact_score")
    op.drop_column("news_posts", "credibility_score")
    op.drop_column("news_posts", "relevance_score")
    op.drop_column("news_posts", "topics")
    op.drop_column("news_posts", "age_relevance")
