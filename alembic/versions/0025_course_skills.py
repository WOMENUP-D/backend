"""Skills for the courses that came from elsewhere, and ten more in Uzbek.

The first load brought in 794 courses with no skills on them. That is not a
cosmetic gap: the recommender ranks a course by how much of it she does not
know yet, read off `skills_taught`, so with the list empty it was choosing
blind inside a category. Three quarters of them name a skill outright in their
title — Python, Excel, English, parenting — and those are now written down.
The rest keep an empty list, because a guessed skill sends a woman somewhere
she did not ask to go.

The same pass adds the ten Uzbek courses that were only ever cards on the
page: in the table they can be filtered by language and offered by the
recommender, which is the whole point of having a catalogue.

Existing rows are matched by slug and only ever have their skills and
description rewritten; a programme of the portal's own — one with no `source`
— is never touched.

Revision ID: 0025
Revises: 0024
"""

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

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


def _records() -> list[dict]:
    if not DATA.exists():
        return []
    out, seen = [], set()
    for record in json.loads(DATA.read_text(encoding="utf-8")):
        title = (record.get("title") or "").strip()[:300]
        url = (record.get("url") or "").strip()
        if not title or not HTTPS.fullmatch(url) or record.get("category") not in CATEGORIES:
            continue
        slug = SLUG_SAFE.sub(
            "-", f"{record.get('source', '')}-{record.get('external_id')}".lower()
        ).strip("-")[:160]
        if not slug or slug in seen:
            continue
        seen.add(slug)
        summary = (record.get("summary") or "").strip()[:600]
        skills = [str(s).strip()[:80] for s in (record.get("skills") or []) if str(s).strip()][:8]
        language = record.get("language") or "ru"
        out.append(
            {
                "slug": slug,
                "title": title,
                "url": url,
                "category": record["category"],
                "language": language if language in LANGUAGES else "ru",
                "provider": (record.get("provider") or record.get("source") or "")[:200],
                "source": (record.get("source") or "")[:40],
                "summary": summary,
                "skills": skills,
            }
        )
    return out


def upgrade() -> None:
    records = _records()
    if not records:
        print("0025: no course file in the image; catalogue left as it is.")
        return

    connection = op.get_bind()
    known = dict(
        connection.execute(
            sa.text("SELECT slug, source FROM programs WHERE slug = ANY(:slugs)"),
            {"slugs": [record["slug"] for record in records]},
        ).all()
    )

    updated = 0
    for record in records:
        if record["slug"] not in known:
            continue
        if known[record["slug"]] is None:
            # One of ours that happens to share the slug. Not an import's business.
            continue
        connection.execute(
            sa.text(
                "UPDATE programs SET skills_taught = :skills, description_i18n = :description"
                " WHERE slug = :slug AND source IS NOT NULL"
            ).bindparams(
                sa.bindparam("skills", type_=postgresql.ARRAY(sa.String)),
                sa.bindparam("description", type_=postgresql.JSONB),
            ),
            {
                "skills": record["skills"],
                "description": (
                    {"uz": record["summary"], "ru": record["summary"], "en": record["summary"]}
                    if record["summary"]
                    else {}
                ),
                "slug": record["slug"],
            },
        )
        updated += 1

    now = datetime.now(UTC)
    fresh = [
        {
            "id": uuid.uuid4(),
            "slug": record["slug"],
            "title_i18n": {"uz": record["title"], "ru": record["title"], "en": record["title"]},
            "goal_i18n": {},
            "description_i18n": (
                {"uz": record["summary"], "ru": record["summary"], "en": record["summary"]}
                if record["summary"]
                else {}
            ),
            "category": record["category"],
            "format": "video",
            "language": record["language"],
            "target_regions": [],
            "target_segments": [],
            "prerequisites": [],
            "learning_outcomes": [],
            "skills_taught": record["skills"],
            "has_certificate": False,
            "provider": record["provider"],
            "external_url": record["url"],
            "source": record["source"],
            "is_published": True,
            "published_at": now,
            "created_at": now,
            "updated_at": now,
        }
        for record in records
        if record["slug"] not in known
    ]

    if fresh:
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
        for start in range(0, len(fresh), 200):
            op.bulk_insert(programs, fresh[start : start + 200])

    print(f"0025: {updated} courses given their skills, {len(fresh)} added.")


def downgrade() -> None:
    # The skills are the only thing this added to rows that already existed,
    # and an empty list is what they held before.
    op.execute(sa.text("UPDATE programs SET skills_taught = '{}' WHERE source IS NOT NULL"))
    op.execute(sa.text("DELETE FROM programs WHERE source IN ('ochiqkurs', 'direktor', 'spbu')"))
