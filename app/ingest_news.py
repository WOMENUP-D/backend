"""Run the AI news ingest by hand: `python -m app.ingest_news`.

The same run the worker makes every eight hours, on a person's say-so instead
of a timer. Two flags:

* `--dry-run` drafts and gates every candidate, prints the verdicts and commits
  nothing. This is what makes `publication_gate` reviewable before anyone
  trusts it: you can read exactly which clause stopped which article before
  turning the schedule on.
* `--limit N` bounds the run. Each item costs a search, a fetch and two model
  calls, so a spot check should be able to stay a spot check.

This bypasses `NEWS_INGEST_ENABLED` — a human at a terminal has already made
that decision — but it does **not** bypass `NEWS_INGEST_AUTO_PUBLISH` or the
publication gate, so running it by hand cannot publish anything the scheduled
job would not have published.

Like `seed_news.py`, this only ever inserts: news rows, one audit row and the
AI interaction traces. It touches no user, no profile and no existing post.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.constants import DataClassification
from app.services.audit_service import record_audit
from app.services.news_ingest import AUDIT_ACTION, ingest_news

logger = logging.getLogger(__name__)


async def _main() -> None:
    from dataclasses import asdict

    from app.core.logging import configure_logging
    from app.db import SessionLocal

    parser = argparse.ArgumentParser(description="Search for news and draft posts.")
    parser.add_argument("--limit", type=int, default=None, help="How many posts to create.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Draft and gate everything, write nothing.",
    )
    args = parser.parse_args()

    configure_logging()
    async with SessionLocal() as session:
        result = await ingest_news(session, limit=args.limit, dry_run=args.dry_run, force=True)
        if not args.dry_run:
            # The same action the worker writes, so a manual run also moves the
            # schedule marker forward. A run is a run.
            await record_audit(
                session,
                action=AUDIT_ACTION,
                entity_type="news_post",
                actor_role="system",
                classification=DataClassification.INTERNAL,
                changes={**asdict(result), "manual": True},
            )
            await session.commit()

    logger.info("news ingest finished", extra={"extra_fields": asdict(result)})
    print(
        f"\n  Koʻrib chiqildi: {result.considered}\n"
        f"  Tayyorlandi:     {result.drafted}\n"
        f"  Eʼlon qilindi:   {result.published}\n"
        f"  Takror:          {result.skipped_duplicate}\n"
        f"  Moderatsiyaga:   {result.skipped_gate}\n"
        f"  Sabab:           {result.reason or '—'}\n"
        + ("  (dry-run: hech narsa saqlanmadi)\n" if args.dry_run else "")
    )


if __name__ == "__main__":
    asyncio.run(_main())
