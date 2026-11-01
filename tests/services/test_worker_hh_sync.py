"""The worker's half of the hh.uz sync: switched off it does nothing; forced
without credentials it says so and leaves the marker that schedules it."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import worker
from app.core.config import settings
from app.models.audit import AuditLog
from app.services import hh_jobs


@pytest_asyncio.fixture
async def worker_sessions(engine, monkeypatch):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(settings, "hh_access_token", None)
    monkeypatch.setattr(settings, "hh_client_id", None)
    monkeypatch.setattr(settings, "hh_client_secret", None)
    return factory


@pytest.mark.asyncio
async def test_switched_off_the_sync_does_not_run(monkeypatch):
    monkeypatch.setattr(settings, "hh_sync_enabled", False)
    assert (await worker.run_hh_sync()).reason == "disabled"


@pytest.mark.asyncio
async def test_forced_without_credentials_it_reports_and_marks_the_attempt(worker_sessions):
    result = await worker.run_hh_sync(force=True)
    assert result.reason == "unconfigured"
    assert result.created == 0

    async with worker_sessions() as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == hh_jobs.AUDIT_ACTION)
                )
            ).scalars()
        )
    phases = sorted(row.changes.get("phase") for row in rows)
    assert phases == ["finished", "started"]
    assert await worker.last_run_at(hh_jobs.AUDIT_ACTION) is not None
