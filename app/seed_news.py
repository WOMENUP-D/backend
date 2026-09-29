"""The starting feed: what a woman reads on the day she registers.

The copy itself lives in `data/news_seed.json` rather than in this module, and
that move is the point of it. A production deployment never runs a seeding
script — it runs migrations — so as long as the feed existed only as a Python
list in here, it existed only on a developer's machine. `0026_seed_news` reads
the same file this loader reads, and the feed is the same on both.

Two editorial rules run through that file and should survive any later edit.
Every post about medicine names its source, because an unsourced health claim
on a state portal is a rumour with a government logo on it. And nothing there
diagnoses, prescribes or promises: the posts explain what is known and send the
reader to a doctor for what is hers.

`adult` marks a post that belongs to adult care. The feed withholds those from
a minor and from a reader whose age is not known — see `services.age_gate`.

A record dates itself one of two ways. `on` is the date the source itself
shows, and everything that names an outside source carries one: re-dating
somebody else's article to today would be a false statement about when it was
written, on a card that names them as the source. `days_ago` counts back from
the moment of loading, and only the portal's own announcements use it — those
are about the portal as it is today, so the day the feed was installed is the
honest date for them.

`published: false` holds a post back. It is there for copy that is written and
correct but describes something the portal cannot show yet — a post pointing at
an empty section promises a woman something that is not there, and that is
worse than a shorter feed.

Nothing carries hand-written age scores. Each post is read by the keyword rules
in `services.news_age` as it is loaded, which is the same reading the feed
would apply to it anyway — so the feed personalises correctly out of the box,
and running the AI editor over it later sharpens those numbers rather than
introducing them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import NewsCategory
from app.models.news import NewsPost
from app.services.news_ai import apply_analysis, rule_based
from app.services.news_ingest import url_hash

logger = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent.parent / "data" / "news_seed.json"


def load_records() -> list[dict]:
    """The shipped feed copy, in the order it was written."""
    return json.loads(DATA.read_text(encoding="utf-8"))


def published_at(record: dict, now: datetime) -> datetime:
    """See the module docstring: the source's own date, or counted back from now."""
    if record.get("on"):
        return datetime.fromisoformat(record["on"]).replace(tzinfo=UTC)
    return now - timedelta(days=record["days_ago"])


def apply_record(post: NewsPost, record: dict, now: datetime) -> None:
    """Write one record onto a post, scoring it from the text just written."""
    post.category = NewsCategory(record["category"])
    post.title_i18n = record["title"]
    post.summary_i18n = record["summary"]
    post.body_i18n = record["body"]
    post.cover_url = record.get("cover_url")
    post.cover_tone = record["tone"]
    post.cover_emblem = record["emblem"]
    post.source_name = record["source_name"]
    post.source_url = record["source_url"]
    # The deduplication key, so the AI ingest never re-posts an article the
    # feed already carries. Null where there is no link to canonicalise, which
    # is what the portal's own announcements have.
    post.source_url_hash = url_hash(record["source_url"]) if record["source_url"] else None
    post.tags = record["tags"]
    post.reading_minutes = record["reading_minutes"]
    post.is_adult_only = record["adult"]
    post.is_pinned = record["pinned"]
    post.is_published = record.get("published", True)
    post.published_at = published_at(record, now)

    # Scored from the text that was just written onto the post, so the ordering
    # of these two statements matters.
    apply_analysis(post, rule_based(post))


async def load_news(session: AsyncSession, now: datetime | None = None) -> int:
    """Write the feed, matching on `slug`.

    Non-destructive on purpose, and separate from `app.seed` for a reason worth
    keeping: the demo seeder clears the tables it owns, and `users` is one of
    them — running it to refresh the feed also deletes every account anyone has
    registered since. Loading the feed must never cost somebody her profile.

    Re-runnable: an existing post is updated in place, so its id and any link
    already shared to it survive.
    """
    now = now or datetime.now(UTC)
    written = 0

    for record in load_records():
        post = await session.scalar(select(NewsPost).where(NewsPost.slug == record["slug"]))
        if post is None:
            post = NewsPost(slug=record["slug"])
            session.add(post)
        apply_record(post, record, now)
        written += 1

    await session.flush()
    return written


async def _main() -> None:
    from app.core.logging import configure_logging
    from app.db import SessionLocal

    configure_logging()
    async with SessionLocal() as session:
        count = await load_news(session)
        await session.commit()
    logger.info("loaded %s news posts", count)
    print(f"\n  {count} ta yangilik yuklandi. Boshqa hech narsa oʻzgartirilmadi.\n")


if __name__ == "__main__":
    asyncio.run(_main())
