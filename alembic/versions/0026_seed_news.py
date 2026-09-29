"""The starting feed, in production.

The portal's front page after registration is the news feed, and in production
it was empty: the twenty-two posts written for it only ever reached a database
through `python -m app.seed_news`, and a deployment runs migrations, not seeding
scripts. So a woman who registered was met by a blank page on the one screen
meant to greet her.

This loads the same `data/news_seed.json` the seeder reads — fifteen posts
written for the portal and seven articles taken from sources the ingest already
trusts (UzA, UN Women, UNFPA, UNICEF), each with its real publication date and
a link back.

Insert-only, and matched on both keys the table has: a slug already present is
left exactly as it is, and so is an article whose source URL the feed already
carries under another slug. Re-running this on a database where an editor has
since rewritten a post does not undo her work.

The editorial score columns are left empty on purpose rather than filled with
numbers from a rule that may change. `news_ranking` reads an unscored post
through `derive_age_relevance` and matches her interests against the post's own
text, so the feed personalises from day one; the AI editor sharpens the numbers
when it runs.

Revision ID: 0026
Revises: 0025
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

DATA = Path(__file__).resolve().parents[2] / "data" / "news_seed.json"

# Repeated here rather than imported: a migration has to keep meaning what it
# meant on the day it ran, and `app.core.constants` is free to move on.
CATEGORIES = {
    "health",
    "medicine",
    "science",
    "education",
    "career",
    "success_story",
    "announcement",
}
TONES = {"plum", "rose", "sand", "sage", "sky", "ink"}


def _url_hash(url: str) -> str | None:
    """The canonical form of the URL, hashed — `news_ingest.url_hash` verbatim.

    Scheme dropped, host lowercased without `www.`, query and fragment removed,
    trailing slash trimmed. A test in `tests/test_news_seed.py` holds this equal
    to the service function it copies.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    canonical = f"{host}{parts.path.rstrip('/')}"
    return hashlib.sha256(canonical.encode()).hexdigest()


def _records() -> list[dict]:
    """The shipped feed, with anything malformed dropped rather than inserted."""
    if not DATA.exists():
        return []

    out: list[dict] = []
    for record in json.loads(DATA.read_text(encoding="utf-8")):
        slug = (record.get("slug") or "").strip()[:120]
        source_name = (record.get("source_name") or "").strip()[:160]
        url = (record.get("source_url") or "").strip()
        if not slug or not source_name or record.get("category") not in CATEGORIES:
            continue
        if url and not url.startswith("https://"):
            continue
        if not all(
            isinstance(record.get(field), dict) and record[field].get("uz")
            for field in ("title", "summary", "body")
        ):
            continue
        tone = record.get("tone")
        out.append(
            {
                "slug": slug,
                "category": record["category"],
                "title": record["title"],
                "summary": record["summary"],
                "body": record["body"],
                "tone": tone if tone in TONES else "plum",
                "emblem": (record.get("emblem") or "✦")[:8],
                "source_name": source_name,
                "source_url": url[:500] or None,
                "hash": _url_hash(url) if url else None,
                "tags": [
                    str(tag).strip()[:60] for tag in (record.get("tags") or []) if str(tag).strip()
                ],
                "minutes": record.get("reading_minutes"),
                "adult": bool(record.get("adult")),
                "pinned": bool(record.get("pinned")),
                "on": record.get("on"),
                "days_ago": record.get("days_ago"),
            }
        )
    return out


def _published_at(record: dict, now: datetime) -> datetime:
    """A real date where the source has one, otherwise counted back from now."""
    if record["on"]:
        return datetime.fromisoformat(record["on"]).replace(tzinfo=UTC)
    return now - timedelta(days=int(record["days_ago"] or 0))


def upgrade() -> None:
    records = _records()
    if not records:
        print("0026: no feed file in the image; news left as it is.")
        return

    connection = op.get_bind()
    taken_slugs = {
        row[0]
        for row in connection.execute(
            sa.text("SELECT slug FROM news_posts WHERE slug = ANY(:slugs)"),
            {"slugs": [record["slug"] for record in records]},
        ).all()
    }
    hashes = [record["hash"] for record in records if record["hash"]]
    taken_hashes = {
        row[0]
        for row in connection.execute(
            sa.text("SELECT source_url_hash FROM news_posts WHERE source_url_hash = ANY(:hashes)"),
            {"hashes": hashes},
        ).all()
    }

    now = datetime.now(UTC)
    fresh = []
    for record in records:
        if record["slug"] in taken_slugs:
            continue
        # The unique index on the hash is what keeps the same article out of the
        # feed twice; a collision here means it is already there under another
        # slug, and the post that is live wins.
        if record["hash"] and record["hash"] in taken_hashes:
            continue
        if record["hash"]:
            taken_hashes.add(record["hash"])
        fresh.append(
            {
                "id": uuid.uuid4(),
                "slug": record["slug"],
                "category": record["category"],
                "title_i18n": record["title"],
                "summary_i18n": record["summary"],
                "body_i18n": record["body"],
                "cover_tone": record["tone"],
                "cover_emblem": record["emblem"],
                "source_name": record["source_name"],
                "source_url": record["source_url"],
                "source_url_hash": record["hash"],
                "tags": record["tags"],
                "reading_minutes": record["minutes"],
                "is_adult_only": record["adult"],
                "is_published": True,
                "is_pinned": record["pinned"],
                "published_at": _published_at(record, now),
                "age_relevance": {},
                "topics": [],
                "ai_meta": {},
                "created_at": now,
                "updated_at": now,
            }
        )

    if not fresh:
        print("0026: the feed already carries every seeded post.")
        return

    # Typed columns, because an untyped JSONB column reaches asyncpg as a dict
    # it has no encoder for.
    news_posts = sa.table(
        "news_posts",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("slug", sa.String),
        sa.column("category", sa.String),
        sa.column("title_i18n", postgresql.JSONB),
        sa.column("summary_i18n", postgresql.JSONB),
        sa.column("body_i18n", postgresql.JSONB),
        sa.column("cover_tone", sa.String),
        sa.column("cover_emblem", sa.String),
        sa.column("source_name", sa.String),
        sa.column("source_url", sa.String),
        sa.column("source_url_hash", sa.String),
        sa.column("tags", postgresql.ARRAY(sa.String)),
        sa.column("reading_minutes", sa.Integer),
        sa.column("is_adult_only", sa.Boolean),
        sa.column("is_published", sa.Boolean),
        sa.column("is_pinned", sa.Boolean),
        sa.column("published_at", sa.DateTime(timezone=True)),
        sa.column("age_relevance", postgresql.JSONB),
        sa.column("topics", postgresql.ARRAY(sa.String)),
        sa.column("ai_meta", postgresql.JSONB),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(news_posts, fresh)
    print(f"0026: {len(fresh)} news posts added to the feed.")


def downgrade() -> None:
    # Only the posts this file ships, and only while nobody has edited them:
    # `updated_at = created_at` is what an untouched insert looks like.
    slugs = [record["slug"] for record in _records()]
    if not slugs:
        return
    op.execute(
        sa.text(
            "DELETE FROM news_posts WHERE slug = ANY(:slugs) AND updated_at = created_at"
        ).bindparams(sa.bindparam("slugs", value=slugs, type_=postgresql.ARRAY(sa.String)))
    )
