"""Background worker: notifications, outbox drain, risk flags, news, hh.uz.

Run alongside the API (`python -m app.worker`). Kept as a simple polling loop
rather than a broker-backed queue — at pilot volume this is enough, and it has
no infrastructure the pilot does not already need.

The eight-hourly news ingest lives here too, and it is the one job on the loop
that costs money and publishes to readers. That changes two things about how
it is scheduled: its last run is persisted in the audit log rather than held in
memory, so a restart does not fire it, and it runs under a Postgres advisory
lock, so two workers cannot search and publish at the same time.

The hh.uz vacancy sync is scheduled the same way, for the same reasons: it
calls a third party's API on a quota and it changes what the jobs page shows.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import settings
from app.core.constants import DataClassification, RiskFlagType
from app.core.logging import configure_logging
from app.db import SessionLocal
from app.models.audit import AuditLog, RiskFlag
from app.models.user import User
from app.services import hh_jobs, retention
from app.services.audit_service import record_audit
from app.services.integration_gateway import drain_outbox
from app.services.news_ingest import AUDIT_ACTION, IngestResult, ingest_news
from app.services.notification_service import dispatch_pending

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
RISK_SCAN_SECONDS = 3600
INACTIVITY_DAYS = 21

# A Postgres advisory lock key, arbitrary but fixed: two workers must not
# search and publish at the same time. A code constant because it is a protocol
# between processes; the interval beside it is an operator dial and comes from
# `settings.news_ingest_interval_hours` instead.
NEWS_INGEST_LOCK = 728_401
HH_SYNC_LOCK = 728_402


async def scan_inactivity() -> int:
    """Raise a risk flag for users inactive beyond the threshold.

    The flag notifies a coordinator. It never restricts the user's access —
    section 06 is explicit that there is no automatic sanction.
    """
    cutoff = datetime.now(UTC) - timedelta(days=INACTIVITY_DAYS)

    async with SessionLocal() as session:
        candidates = list(
            (
                await session.execute(
                    select(User).where(
                        User.last_active_at.is_not(None),
                        User.last_active_at < cutoff,
                        User.onboarding_completed_at.is_not(None),
                    )
                )
            ).scalars()
        )

        raised = 0
        for user in candidates:
            already = await session.scalar(
                select(RiskFlag.id).where(
                    RiskFlag.user_id == user.id,
                    RiskFlag.flag_type == RiskFlagType.INACTIVITY,
                    RiskFlag.resolved_at.is_(None),
                )
            )
            if already:
                continue
            session.add(
                RiskFlag(
                    user_id=user.id,
                    flag_type=RiskFlagType.INACTIVITY,
                    severity="medium",
                    reason=f"No activity for more than {INACTIVITY_DAYS} days",
                    evidence={"last_active_at": user.last_active_at.isoformat()},
                )
            )
            raised += 1

        await session.commit()
        return raised


def next_due(
    previous: datetime | None,
    *,
    interval_seconds: int,
    now_mono: float,
    now: datetime,
) -> float:
    """When the next ingest is due, on the loop's monotonic clock.

    `previous` is the last recorded attempt, read out of the audit log rather
    than held in memory: the in-memory `last_risk_scan = 0.0` pattern beside it
    fires on every worker start, which is harmless for a database scan and is
    not harmless for a paid job that publishes. `None` — never run at all —
    means fire on the first tick, the one case where an immediate run is right.

    Pure and separate from the loop because this is the arithmetic that decides
    whether a crash-looping worker publishes on every boot, and that deserves a
    test rather than a comment.
    """
    if previous is None:
        return now_mono
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    elapsed = (now - previous).total_seconds()
    return now_mono + max(0.0, interval_seconds - elapsed)


async def last_run_at(action: str) -> datetime | None:
    """When a scheduled job last ran, read back from the audit log.

    Persisted rather than held in memory, and deliberately not in a new table:
    every run already writes an insert-only audit row under its action, so the
    latest one *is* the schedule state.

    Reading a maximum is not an update: the insert-only contract on `AuditLog`
    holds unchanged.
    """
    async with SessionLocal() as session:
        return await session.scalar(
            select(func.max(AuditLog.created_at)).where(AuditLog.action == action)
        )


async def last_ingest_at() -> datetime | None:
    """When the news ingest last ran."""
    return await last_run_at(AUDIT_ACTION)


async def mark_attempt(action: str, entity_type: str) -> None:
    """Record — and commit — that a run of `action` is about to happen."""
    async with SessionLocal() as session, session.begin():
        await record_audit(
            session,
            action=action,
            entity_type=entity_type,
            actor_id=None,
            actor_role="system",
            classification=DataClassification.INTERNAL,
            changes={"phase": "started"},
        )


async def mark_ingest_attempt() -> None:
    """Record — and commit — that a run is about to happen.

    Its own transaction, and before the work, which is the only arrangement
    that makes this usable as the schedule marker. A row written inside the
    run's transaction would be discarded by the very failure that makes
    re-firing dangerous, and a crash-looping worker would then start a paid,
    publishing job on every boot.

    Which failures still re-fire, plainly: a database unreachable at this exact
    point (nothing is written — but then nothing runs either, because the run
    needs the same database, and `last_ingest_at` would have failed first), and
    an `audit_logs` table restored from a backup older than the last run.
    Neither is silent. Everything after this line — a crash mid-search, a
    killed container, an unparsable reply — leaves the marker in place and
    waits for the next interval.
    """
    await mark_attempt(AUDIT_ACTION, "news_post")


async def run_news_ingest() -> IngestResult:
    """One ingest run, under an advisory lock, isolated from the loop.

    Returns a result rather than raising: `ingest_news` already degrades
    internally, and anything that still escapes is caught here so a failed
    search can never stop notification delivery or the outbox drain.
    """
    try:
        await mark_ingest_attempt()

        async with SessionLocal() as session, session.begin():
            # `pg_try_advisory_xact_lock`, not `pg_try_advisory_lock`. The
            # session-level form is tied to a *connection*; committing returns
            # that connection to the pool, so the matching unlock would run on
            # whichever connection came out next, return false, and leave the
            # lock held on a pooled connection indefinitely — after which every
            # later run logs "already running elsewhere" and the job is dead.
            # The transaction-scoped form is released by this block's COMMIT or
            # ROLLBACK, whichever happens, and by the backend exiting if the
            # worker is killed.
            locked = await session.scalar(select(func.pg_try_advisory_xact_lock(NEWS_INGEST_LOCK)))
            if not locked:
                logger.info("news ingest already running elsewhere; skipping")
                return IngestResult(reason="locked")

            result = await ingest_news(session)
            await record_audit(
                session,
                action=AUDIT_ACTION,
                entity_type="news_post",
                actor_id=None,
                actor_role="system",
                classification=DataClassification.INTERNAL,
                changes={**asdict(result), "phase": "finished"},
            )
            return result
    except Exception:
        logger.exception("news ingest failed; the feed is unchanged")
        return IngestResult(reason="error")


async def run_hh_sync(*, force: bool = False) -> hh_jobs.SyncResult:
    """One hh.uz sync, under its own advisory lock, isolated from the loop.

    `force` is the operator's `python -m app.hh_sync`: it runs whether or not
    the scheduled sync is switched on. Returns a result rather than raising,
    so a feed that is down can never stop notification delivery.
    """
    if not force and not settings.hh_sync_enabled:
        return hh_jobs.SyncResult(reason="disabled")
    try:
        await mark_attempt(hh_jobs.AUDIT_ACTION, "opportunity")

        async with SessionLocal() as session, session.begin():
            locked = await session.scalar(select(func.pg_try_advisory_xact_lock(HH_SYNC_LOCK)))
            if not locked:
                logger.info("hh.uz sync already running elsewhere; skipping")
                return hh_jobs.SyncResult(reason="locked")

            result = await hh_jobs.sync_from_settings(session)
            await record_audit(
                session,
                action=hh_jobs.AUDIT_ACTION,
                entity_type="opportunity",
                actor_id=None,
                actor_role="system",
                classification=DataClassification.INTERNAL,
                changes={**asdict(result), "phase": "finished"},
            )
            return result
    except Exception:
        logger.exception("hh.uz sync failed; the catalogue is unchanged")
        return hh_jobs.SyncResult(reason="error")


async def run() -> None:
    configure_logging()
    logger.info("worker started (poll=%ss)", POLL_SECONDS)
    last_risk_scan = 0.0
    # Resolved on the first tick that reaches a database, not here. Reading it
    # before the loop would put a query outside the loop's try/except, and a
    # worker that starts before Postgres does would die instead of retrying —
    # which is exactly what `run()` survives today.
    news_due_at: float | None = None
    hh_due_at: float | None = None

    while True:
        try:
            async with SessionLocal() as session:
                delivered = await dispatch_pending(session)
                sent, failed = await drain_outbox(session)
                await session.commit()

            if heartbeat := os.getenv("WORKER_HEARTBEAT_FILE"):
                Path(heartbeat).touch()

            if delivered or sent or failed:
                logger.info(
                    "worker tick",
                    extra={
                        "extra_fields": {
                            "notifications": delivered,
                            "integration_sent": sent,
                            "integration_failed": failed,
                        }
                    },
                )

            now = asyncio.get_running_loop().time()
            if now - last_risk_scan >= RISK_SCAN_SECONDS:
                raised = await scan_inactivity()
                last_risk_scan = now
                if raised:
                    logger.info("raised %s inactivity flags", raised)
                # The same hourly tick retires what has outlived its purpose:
                # old assistant excerpts, spent codes, expired bookkeeping.
                async with SessionLocal() as session:
                    purged = await retention.purge(session)
                    await session.commit()
                logger.info("retention sweep", extra={"extra_fields": asdict(purged)})

            # Read from `settings` here rather than at import so the switch can
            # be flipped with a worker restart and nothing else.
            if settings.news_ingest_enabled:
                interval = max(1, settings.news_ingest_interval_hours) * 3600
                if news_due_at is None:
                    news_due_at = next_due(
                        await last_ingest_at(),
                        interval_seconds=interval,
                        now_mono=now,
                        now=datetime.now(UTC),
                    )
                if now >= news_due_at:
                    # Rescheduled before the run, not after: a run that fails
                    # or takes twenty minutes must not be retried on the next
                    # 30-second poll, and must not drift the cadence later
                    # every day.
                    news_due_at = now + interval
                    result = await run_news_ingest()
                    logger.info("news ingest", extra={"extra_fields": asdict(result)})

            if settings.hh_sync_enabled:
                interval = max(1, settings.hh_sync_interval_hours) * 3600
                if hh_due_at is None:
                    hh_due_at = next_due(
                        await last_run_at(hh_jobs.AUDIT_ACTION),
                        interval_seconds=interval,
                        now_mono=now,
                        now=datetime.now(UTC),
                    )
                if now >= hh_due_at:
                    hh_due_at = now + interval
                    synced = await run_hh_sync()
                    logger.info("hh.uz sync", extra={"extra_fields": asdict(synced)})

        except Exception:
            logger.exception("worker tick failed; continuing")

        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run())
