"""The feed a woman meets on her first day.

These hold two things the feed cannot be trusted without. The shipped file has
to be publishable — attributed, complete in Uzbek, and clear of the same bar the
AI ingest has to clear before anything reaches a reader unread. And the loader
and the migration have to agree about it, because one of them fills a
developer's database and the other fills production, and a feed that differs
between the two is a feed nobody can reason about.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.constants import NewsCategory
from app.models.news import NewsPost
from app.schemas.news import COVER_TONES
from app.seed_news import load_news, load_records, published_at
from app.services.news_ai import rule_based
from app.services.news_ingest import publication_gate, url_hash

RECORDS = load_records()
DATED = [record for record in RECORDS if record.get("on")]

# The migration is loaded by path: `0026_seed_news` is not an importable module
# name, and these tests are about the file that actually runs on the server.
_spec = importlib.util.spec_from_file_location(
    "seed_news_migration",
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0026_seed_news.py",
)
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


# ------------------------------------------------------------ the file


def test_the_feed_is_not_empty():
    assert len(RECORDS) >= 20
    # Both dating styles are in use, and each is right for its half.
    assert DATED, "no post carries a real publication date"
    assert [record for record in RECORDS if not record.get("on")]


@pytest.mark.parametrize("record", RECORDS, ids=[record["slug"] for record in RECORDS])
def test_every_post_is_publishable(record):
    assert len(record["slug"]) <= 120
    assert record["source_name"], "a post with no source named"
    assert NewsCategory(record["category"])
    assert record["tone"] in COVER_TONES
    assert len(record["emblem"]) <= 8
    # Uzbek is the primary locale; a post the portal cannot show in it is not
    # finished, whatever else it got right.
    for field in ("title", "summary", "body"):
        assert set(record[field]) == {"uz", "ru", "en"}
        assert all(text.strip() for text in record[field].values())
    # One dating style or the other, never both and never neither.
    assert bool(record.get("on")) != ("days_ago" in record)
    if record["source_url"]:
        assert record["source_url"].startswith("https://")
        assert len(record["source_url"]) <= 500


def test_medicine_and_science_link_to_the_source():
    """An unsourced health claim on a state portal is a rumour with a logo on it."""
    needs_a_link = {"medicine", "health", "science"}
    unlinked = [
        record["slug"]
        for record in RECORDS
        if record["category"] in needs_a_link and not record["source_url"]
    ]
    assert unlinked == []


def test_no_two_posts_carry_the_same_article():
    slugs = [record["slug"] for record in RECORDS]
    assert len(slugs) == len(set(slugs))
    hashes = [url_hash(record["source_url"]) for record in RECORDS if record["source_url"]]
    assert len(hashes) == len(set(hashes))


@pytest.mark.parametrize("record", DATED, ids=[record["slug"] for record in DATED])
def test_a_post_taken_from_a_source_clears_the_ingest_gate(record):
    """The same bar the AI has to clear, applied to what a human seeded.

    `category_needs_review` is the one allowed answer: announcements are held
    for a human by design, and these had one.
    """
    post = NewsPost(
        slug=record["slug"],
        category=NewsCategory(record["category"]),
        title_i18n=record["title"],
        summary_i18n=record["summary"],
        body_i18n=record["body"],
        source_name=record["source_name"],
        source_url=record["source_url"],
        tags=record["tags"],
    )
    assert set(publication_gate(post, rule_based(post))) <= {"category_needs_review"}


def test_a_dated_post_is_not_dated_in_the_future():
    for record in DATED:
        assert published_at(record, NOW) <= NOW, record["slug"]


# ------------------------------------------------------------ loader vs migration


def test_the_migration_reads_every_shipped_post():
    """Anything the migration quietly drops would be missing in production only."""
    assert [record["slug"] for record in migration._records()] == [
        record["slug"] for record in RECORDS
    ]


def test_the_two_agree_about_dates():
    for mine, theirs in zip(RECORDS, migration._records(), strict=True):
        assert published_at(mine, NOW) == migration._published_at(theirs, NOW), mine["slug"]


def test_the_migrations_copy_of_the_hash_is_the_real_one():
    """It is copied on purpose — a migration must keep meaning what it meant."""
    for record in RECORDS:
        if record["source_url"]:
            assert migration._url_hash(record["source_url"]) == url_hash(record["source_url"])
    assert migration._url_hash("not-a-url") is None


# ------------------------------------------------------------ against a database


@pytest.mark.asyncio
async def test_loading_the_feed_publishes_every_post(session):
    written = await load_news(session, NOW)
    assert written == len(RECORDS)

    published = await session.scalar(
        select(func.count()).select_from(NewsPost).where(NewsPost.is_published.is_(True))
    )
    assert published == len(RECORDS)


@pytest.mark.asyncio
async def test_a_dated_post_keeps_its_real_date(session):
    await load_news(session, NOW)
    record = DATED[0]
    post = await session.scalar(select(NewsPost).where(NewsPost.slug == record["slug"]))
    assert post.published_at.date().isoformat() == record["on"]
    assert post.source_url_hash == url_hash(record["source_url"])


@pytest.mark.asyncio
async def test_the_portals_own_posts_carry_no_source_hash(session):
    """Null, and Postgres allows many nulls in a unique index — which is the
    only reason fifteen unlinked posts can coexist under one."""
    await load_news(session, NOW)
    unlinked = await session.scalars(select(NewsPost).where(NewsPost.source_url.is_(None)).limit(3))
    for post in unlinked:
        assert post.source_url_hash is None


@pytest.mark.asyncio
async def test_loading_twice_does_not_duplicate_the_feed(session):
    await load_news(session, NOW)
    await load_news(session, NOW)
    total = await session.scalar(select(func.count()).select_from(NewsPost))
    assert total == len(RECORDS)


@pytest.mark.asyncio
async def test_a_loaded_post_is_scored_for_ordering(session):
    """Not by hand and not by a model: read off the post's own text as it loads,
    so the personalised section works on a portal with no API key at all."""
    await load_news(session, NOW)
    post = await session.scalar(select(NewsPost).where(NewsPost.slug == RECORDS[0]["slug"]))
    assert post.age_relevance, "nothing scored the post for age"
    assert post.relevance_score is not None
