"""The worker's half of the news ingest: the lock, and the schedule marker.

Two failures live here, and both of them are silent — the job simply stops
happening and nothing anywhere says so.

The first is the advisory lock. `pg_try_advisory_lock` is session-level, which
means connection-scoped, and committing hands that connection back to the pool;
the matching unlock then runs on whatever connection came out next, returns
false, and the lock stays held on a pooled connection forever. Every later run
logs "already running elsewhere" and does nothing. The transaction-scoped form
cannot leak that way, and the test for it is simply: run it twice.

The second is the marker. A worker that crash-loops must not fire a paid,
publishing job on every boot, so the marker has to be committed before the work
and independently of it — a row written inside the run's own transaction is
discarded by the very failure that makes re-firing dangerous.

These use the real `SessionLocal` code path, redirected at the test database.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import worker
from app.core.config import settings
from app.models.audit import AuditLog
from app.models.news import NewsPost
from app.services.llm_gateway import LlmUnavailableError
from app.services.news_ingest import ingest_news
from tests.api.test_news_ingest import _one_who_article


@pytest_asyncio.fixture
async def worker_sessions(engine, monkeypatch):
    """Point the worker's own session factory at the test database.

    `run_news_ingest` deliberately opens its own sessions and commits them —
    that is what makes the marker independent — so it cannot share the test's
    rolled-back session and still be the thing under test.
    """
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(settings, "news_ingest_enabled", True)
    monkeypatch.setattr(settings, "news_ingest_auto_publish", True)
    return factory


async def _rows(factory, model):
    async with factory() as session:
        return list((await session.execute(select(model))).scalars())


@pytest.mark.asyncio
async def test_two_consecutive_runs_both_acquire_the_lock(worker_sessions, monkeypatch):
    """The regression test for a leaked advisory lock. If the lock were
    session-level and released on the wrong connection, this second run would
    come back `locked` and the job would be dead for the life of the process."""
    monkeypatch.setattr("app.services.news_ingest.llm_gateway", _one_who_article())
    first = await worker.run_news_ingest()

    monkeypatch.setattr("app.services.news_ingest.llm_gateway", _one_who_article())
    second = await worker.run_news_ingest()

    assert first.reason is None
    assert first.published == 1
    assert second.reason != "locked"
    # Same article, so the second run finds nothing new — which is the correct
    # outcome and a different one from "could not start".
    assert second.skipped_duplicate == 1
    assert len(await _rows(worker_sessions, NewsPost)) == 1


@pytest.mark.asyncio
async def test_a_completed_run_leaves_a_marker_the_schedule_can_read(worker_sessions, monkeypatch):
    monkeypatch.setattr("app.services.news_ingest.llm_gateway", _one_who_article())

    await worker.run_news_ingest()

    assert await worker.last_ingest_at() is not None
    phases = [row.changes.get("phase") for row in await _rows(worker_sessions, AuditLog)]
    assert phases == ["started", "finished"]


@pytest.mark.asyncio
async def test_a_run_that_blows_up_still_leaves_its_marker(worker_sessions, monkeypatch):
    """The one that matters. Without a marker written and committed before the
    work, a crash-looping worker starts a paid, publishing run on every boot —
    which is exactly the failure the marker exists to prevent."""

    async def explode(*_args, **_kwargs):
        raise RuntimeError("the search machinery fell over")

    monkeypatch.setattr("app.services.news_ingest.llm_gateway", _one_who_article())
    monkeypatch.setattr(worker, "ingest_news", explode)

    result = await worker.run_news_ingest()

    assert result.reason == "error"
    assert await worker.last_ingest_at() is not None
    assert await _rows(worker_sessions, NewsPost) == []


@pytest.mark.asyncio
async def test_the_lock_is_free_again_after_a_run_that_blew_up(worker_sessions, monkeypatch):
    """A transaction-scoped lock is released by the rollback the exception
    causes, so a failed run does not disable the next one."""

    async def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.news_ingest.llm_gateway", _one_who_article())
    monkeypatch.setattr(worker, "ingest_news", explode)
    assert (await worker.run_news_ingest()).reason == "error"

    monkeypatch.setattr(worker, "ingest_news", ingest_news)
    monkeypatch.setattr("app.services.news_ingest.llm_gateway", _one_who_article())
    second = await worker.run_news_ingest()

    assert second.reason != "locked"
    assert second.published == 1


@pytest.mark.asyncio
async def test_a_degraded_run_records_why_rather_than_recording_nothing(
    worker_sessions, monkeypatch
):
    """A run that found nothing is still a run: it writes the marker and says
    what happened, so an operator reading the audit log can tell "the model was
    unreachable" from "nobody ever turned this on"."""

    class Unreachable:
        enabled = True

        async def complete(self, **_kwargs):
            raise LlmUnavailableError("model rate limit reached")

    monkeypatch.setattr("app.services.news_ingest.llm_gateway", Unreachable())

    result = await worker.run_news_ingest()

    assert result.reason == "model_unavailable"
    finished = [
        row.changes
        for row in await _rows(worker_sessions, AuditLog)
        if row.changes.get("phase") == "finished"
    ]
    assert finished == [
        {
            "phase": "finished",
            "considered": 0,
            "drafted": 0,
            "published": 0,
            "skipped_duplicate": 0,
            "skipped_gate": 0,
            "reason": "model_unavailable",
        }
    ]
