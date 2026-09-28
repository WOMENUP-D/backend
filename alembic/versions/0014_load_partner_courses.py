"""Put the partner courses in the catalogue.

`app.import_courses` is the tool for doing this whenever the list changes. This
migration does it once, at deployment, for the first load — because the
alternative was a person with SSH running a command by hand on a live server,
and the catalogue has been empty for long enough.

It is written to be safe to run against a catalogue that already has things in
it: a course is inserted only when its slug is absent, so nothing is
overwritten and a re-run inserts nothing. The downgrade removes only the rows
this file put there, matched on `source`, and never touches a programme of the
portal's own.

The data is read from `data/partner_courses.json` beside the application. If
the file is not in the image the migration does nothing and says so rather
than failing a deployment over a catalogue of somebody else's courses.

Revision ID: 0014
Revises: 0013
"""

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

SOURCE = "stepik"
DATA = Path(__file__).resolve().parents[2] / "data" / "partner_courses.json"
SLUG_SAFE = re.compile(r"[^a-z0-9]+")
HTTPS = re.compile(r"^https://[^\s\"']+$")

CATEGORIES = {
    "vocational_skills",
    "ethics_culture",
    "health",
    "international",
    "parenting",
    "financial_literacy",
    "entrepreneurship",
    "leadership",
    "legal_literacy",
    "digital_safety",
    "mentorship_networking",
    "volunteering",
}
LANGUAGES = {"uz", "ru", "en"}


def _rows() -> list[dict]:
    """The file, turned into rows this table accepts. Bad records are skipped."""
    if not DATA.exists():
        return []
    records = json.loads(DATA.read_text(encoding="utf-8"))
    now = datetime.now(UTC)
    rows, seen = [], set()
    for record in records:
        title = (record.get("title") or "").strip()[:300]
        url = (record.get("url") or "").strip()
        category = record.get("category")
        language = record.get("language") or "ru"
        if not title or not HTTPS.fullmatch(url) or category not in CATEGORIES:
            continue
        slug = SLUG_SAFE.sub(
            "-", f"{record.get('source', SOURCE)}-{record.get('external_id')}".lower()
        ).strip("-")[:160]
        if not slug or slug in seen:
            continue
        seen.add(slug)
        summary = (record.get("summary") or "").strip()[:600]
        rows.append(
            {
                "id": uuid.uuid4(),
                "slug": slug,
                "title_i18n": {"uz": title, "ru": title, "en": title},
                "goal_i18n": {},
                "description_i18n": (
                    {"uz": summary, "ru": summary, "en": summary} if summary else {}
                ),
                "category": category,
                "format": "video",
                "language": language if language in LANGUAGES else "ru",
                "target_regions": [],
                "target_segments": [],
                "prerequisites": [],
                "learning_outcomes": [],
                "skills_taught": [],
                "has_certificate": False,
                "provider": (record.get("provider") or SOURCE)[:200],
                "external_url": url,
                "source": (record.get("source") or SOURCE)[:40],
                "is_published": True,
                "published_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


def upgrade() -> None:
    rows = _rows()
    if not rows:
        print("0014: no partner course file in the image; catalogue left as it is.")
        return

    connection = op.get_bind()
    existing = {
        slug
        for (slug,) in connection.execute(
            sa.text("SELECT slug FROM programs WHERE slug = ANY(:slugs)"),
            {"slugs": [row["slug"] for row in rows]},
        )
    }
    fresh = [row for row in rows if row["slug"] not in existing]
    if not fresh:
        print("0014: partner courses already in the catalogue; nothing inserted.")
        return

    # The columns have to carry their types: JSONB and text[] cannot be sent as
    # bare Python dicts and lists, and an untyped insert fails on the first row.
    programs = sa.table(
        "programs",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("slug", sa.String),
        sa.column("title_i18n", postgresql.JSONB),
        sa.column("goal_i18n", postgresql.JSONB),
        sa.column("description_i18n", postgresql.JSONB),
        sa.column("category", sa.String),
        sa.column("format", sa.String),
        sa.column("language", sa.String),
        sa.column("target_regions", postgresql.ARRAY(sa.String)),
        sa.column("target_segments", postgresql.ARRAY(sa.String)),
        sa.column("prerequisites", postgresql.ARRAY(sa.String)),
        sa.column("learning_outcomes", postgresql.JSONB),
        sa.column("skills_taught", postgresql.ARRAY(sa.String)),
        sa.column("has_certificate", sa.Boolean),
        sa.column("provider", sa.String),
        sa.column("external_url", sa.String),
        sa.column("source", sa.String),
        sa.column("is_published", sa.Boolean),
        sa.column("published_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    # In batches, so one statement does not carry eight hundred rows.
    for start in range(0, len(fresh), 200):
        op.bulk_insert(programs, fresh[start : start + 200])
    print(f"0014: inserted {len(fresh)} partner courses.")


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM programs WHERE source = :source").bindparams(source=SOURCE))
