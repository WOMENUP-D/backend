"""Load somebody else's courses into the catalogue.

    python -m app.import_courses data/partner_courses.json

The portal's own programmes are written by hand and take time. Until they
exist the catalogue is empty, and an empty catalogue teaches nobody anything —
so it also lists open courses from elsewhere: Stepik, a university, a local
Uzbek platform. They are ordinary rows in `programs` with two fields set,
`external_url` and `source`, which is what tells the portal they are not ours:
the card links out and offers no enrolment.

Run it as often as you like. Courses are matched by slug, which is built from
the source and the course's own id there, so a second run updates what changed
and adds what is new. It will not touch a programme that has no `source` — the
portal's own work is never overwritten by an import.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import Language, ProgramCategory, ProgramFormat
from app.db import SessionLocal, engine
from app.models.program import Program

#: Anything else in a record is ignored; these five have to be there.
REQUIRED = ("source", "external_id", "title", "url", "category")

#: Only ever https: a course card that drops a reader onto an unencrypted page
#: is not something this portal should recommend.
URL = re.compile(r"^https://[^\s\"']+$")

SLUG_SAFE = re.compile(r"[^a-z0-9]+")


class ImportError_(Exception):
    """Something in the file is wrong; nothing is written."""


def slug_for(source: str, external_id: str) -> str:
    """`stepik-63054`. Stable, so a second run finds the same row."""
    return SLUG_SAFE.sub("-", f"{source}-{external_id}".lower()).strip("-")[:160]


def clean(record: dict, index: int) -> dict:
    """Check one record and put it in the shape the table wants."""
    missing = [key for key in REQUIRED if not str(record.get(key) or "").strip()]
    if missing:
        raise ImportError_(f"record {index}: missing {', '.join(missing)}")

    url = record["url"].strip()
    if not URL.fullmatch(url):
        raise ImportError_(f"record {index}: the address must be https ({record['title'][:40]})")

    try:
        category = ProgramCategory(record["category"])
    except ValueError as exc:
        raise ImportError_(f"record {index}: unknown category {record['category']!r}") from exc

    language = Language(record.get("language") or "ru")
    title = record["title"].strip()[:300]
    summary = (record.get("summary") or "").strip()[:600]

    # Only skills the course names outright. The recommender ranks on "how much
    # of this she does not know yet", so an invented skill sends her somewhere
    # she did not ask to go — an empty list is the honest answer.
    skills = [
        str(skill).strip()[:80] for skill in (record.get("skills") or []) if str(skill).strip()
    ]

    return {
        "slug": slug_for(record["source"], str(record["external_id"])),
        # The title stays in the language it is taught in — translating a course
        # name would invent a course that does not exist under that name.
        "title_i18n": {"uz": title, "ru": title, "en": title},
        "description_i18n": ({"uz": summary, "ru": summary, "en": summary} if summary else {}),
        "category": category,
        "language": language,
        "format": ProgramFormat.VIDEO,
        "provider": (record.get("provider") or record["source"]).strip()[:200],
        "skills_taught": skills[:8],
        "external_url": url,
        "source": record["source"].strip()[:40],
        "is_published": True,
    }


async def load(session: AsyncSession, records: list[dict]) -> tuple[int, int]:
    """Upsert every record. Returns (added, updated)."""
    added = updated = 0
    for row in records:
        existing = await session.scalar(select(Program).where(Program.slug == row["slug"]))
        if existing is None:
            session.add(Program(**row))
            added += 1
            continue
        if existing.source is None:
            # A programme of the portal's own that happens to share the slug.
            # Imports do not get to touch those.
            continue
        for field, value in row.items():
            setattr(existing, field, value)
        updated += 1
    await session.flush()
    return added, updated


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.import_courses",
        description="Load open courses from other platforms into the catalogue.",
    )
    parser.add_argument("file", type=Path, help="JSON list of courses")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check the file and report what would change, write nothing",
    )
    arguments = parser.parse_args(argv)

    try:
        records = json.loads(arguments.file.read_text(encoding="utf-8"))
        if not isinstance(records, list) or not records:
            raise ImportError_("the file must hold a non-empty list of courses")
        rows = [clean(record, index) for index, record in enumerate(records)]
    except (OSError, json.JSONDecodeError, ImportError_) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2

    seen: dict[str, int] = {}
    for index, row in enumerate(rows):
        if row["slug"] in seen:
            print(f"Refused: two records share the slug {row['slug']}", file=sys.stderr)
            return 2
        seen[row["slug"]] = index

    if arguments.dry_run:
        print(f"{len(rows)} courses read, every record valid. Nothing written.")
        return 0

    try:
        async with SessionLocal() as session:
            added, updated = await load(session, rows)
            await session.commit()
    finally:
        await engine.dispose()

    print(f"Added {added}, updated {updated}, read {len(rows)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
