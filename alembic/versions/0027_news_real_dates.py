"""Give the seeded posts the date their source shows, and hold back two of them.

Ten posts in the feed explain what is known — the WHO fact sheets, three Nobel
prizes, the UNESCO figure on women in research. They are sourced and correct,
but their date was counted back from the day the feed was loaded, so the card
about Katalin Karikó's 2023 Nobel was stamped three days ago. A date on a card
that names a source is a claim about that source, and that one was false. Each
now carries the date its own page shows.

Two links were repointed in the same pass, because a source has to be reachable
to be a source. WHO retired the ELENA library, so the folic-acid post pointed
at a 404; it now points at the 2015 guideline on folate in women of reproductive
age. The cervical-cancer post pointed at an initiative landing page that carries
no date of its own; the fact sheet states the same 90-70-90 targets and is dated.

And two posts are withdrawn: both send a reader to the Opportunities section,
which holds nothing yet. Written and correct, but a post that promises an empty
page is worse than a shorter feed. `published: false` in the data file keeps
them out until there is something there, and they come back by flipping it.

Only rows nobody has edited are touched — `updated_at = created_at` is what an
untouched insert looks like, so an editor's rewrite of a seeded post survives
this.

Revision ID: 0027
Revises: 0026
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

DATA = Path(__file__).resolve().parents[2] / "data" / "news_seed.json"


def _url_hash(url: str) -> str | None:
    """`news_ingest.url_hash`, copied so this file keeps meaning what it meant."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    return hashlib.sha256(f"{host}{parts.path.rstrip('/')}".encode()).hexdigest()


def _records(now: datetime) -> list[dict]:
    """Slug, the date to stamp, the link and whether it may be shown."""
    if not DATA.exists():
        return []

    out = []
    for record in json.loads(DATA.read_text(encoding="utf-8")):
        slug = (record.get("slug") or "").strip()[:120]
        if not slug:
            continue
        url = (record.get("source_url") or "").strip()
        if url and not url.startswith("https://"):
            continue
        if record.get("on"):
            published_at = datetime.fromisoformat(record["on"]).replace(tzinfo=UTC)
        else:
            published_at = now - timedelta(days=int(record.get("days_ago") or 0))
        out.append(
            {
                "slug": slug,
                "published_at": published_at,
                "source_url": url[:500] or None,
                "hash": _url_hash(url) if url else None,
                "published": bool(record.get("published", True)),
            }
        )
    return out


def upgrade() -> None:
    records = _records(datetime.now(UTC))
    if not records:
        print("0027: no feed file in the image; news left as it is.")
        return

    connection = op.get_bind()
    # Who holds which hash right now, so a repointed link cannot collide with a
    # post that already carries that article.
    held = {
        row[1]: row[0]
        for row in connection.execute(
            sa.text(
                "SELECT slug, source_url_hash FROM news_posts WHERE source_url_hash IS NOT NULL"
            )
        ).all()
    }

    synced = withdrawn = 0
    for record in records:
        if record["hash"] and held.get(record["hash"], record["slug"]) != record["slug"]:
            print(
                f"0027: {record['slug']} keeps its link — {held[record['hash']]} has that article."
            )
            continue
        result = connection.execute(
            sa.text(
                "UPDATE news_posts SET published_at = :published_at, source_url = :source_url,"
                " source_url_hash = :hash, is_published = :published"
                " WHERE slug = :slug AND updated_at = created_at"
            ),
            record,
        )
        synced += result.rowcount or 0
        if result.rowcount and not record["published"]:
            withdrawn += 1

    print(f"0027: {synced} posts dated from their source, {withdrawn} withdrawn.")


def downgrade() -> None:
    # The dates before this were "a few days before the feed was loaded", which
    # is not a state worth restoring — but a withdrawn post can be shown again,
    # and that is the half of this a rollback actually needs to undo.
    slugs = [record["slug"] for record in _records(datetime.now(UTC)) if not record["published"]]
    if not slugs:
        return
    op.execute(
        sa.text(
            "UPDATE news_posts SET is_published = true"
            " WHERE slug = ANY(:slugs) AND updated_at = created_at"
        ).bindparams(sa.bindparam("slugs", value=slugs, type_=postgresql.ARRAY(sa.String)))
    )
