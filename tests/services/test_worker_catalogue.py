"""The worker loads the shipped partner courses on its first tick."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import worker
from app.models.program import Program
from app.models.skill import Skill

COURSE = {
    "source": "coursera",
    "external_id": "test-course",
    "title": "A Test Course",
    "url": "https://www.coursera.org/learn/test-course",
    "category": "leadership",
    "language": "en",
    "provider": "Test University",
    "summary": "A course that exists only in this test.",
    "skills": ["лидерство"],
}


@pytest_asyncio.fixture
async def worker_sessions(engine, monkeypatch):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    return factory


@pytest.mark.asyncio
async def test_the_shipped_courses_are_upserted(worker_sessions, monkeypatch, tmp_path: Path):
    file = tmp_path / "courses.json"
    file.write_text(json.dumps([COURSE]), encoding="utf-8")
    monkeypatch.setattr(worker, "CATALOGUE_FILE", file)

    assert await worker.run_catalogue_import() == (1, 0)
    assert await worker.run_catalogue_import() == (0, 1)

    async with worker_sessions() as session:
        row = await session.scalar(select(Program).where(Program.slug == "coursera-test-course"))
    assert row is not None and row.external_url == COURSE["url"] and row.is_published


@pytest.mark.asyncio
async def test_a_refused_file_is_logged_not_raised(worker_sessions, monkeypatch, tmp_path: Path):
    file = tmp_path / "courses.json"
    file.write_text(json.dumps([{**COURSE, "url": "http://insecure.example"}]), encoding="utf-8")
    monkeypatch.setattr(worker, "CATALOGUE_FILE", file)
    assert await worker.run_catalogue_import() is None


@pytest.mark.asyncio
async def test_no_file_means_nothing_to_do(worker_sessions, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(worker, "CATALOGUE_FILE", tmp_path / "absent.json")
    assert await worker.run_catalogue_import() is None


def test_the_default_file_is_the_one_shipped_beside_the_code():
    # Relative to the working directory, as in the image (/app) and in this
    # repository; a path taken from `__file__` would point into site-packages
    # once the package is installed, which is how the first release missed it.
    assert Path("data/partner_courses.json") == worker.CATALOGUE_FILE
    assert worker.CATALOGUE_FILE.exists()


@pytest.mark.asyncio
async def test_the_taxonomy_ships_with_the_code(worker_sessions):
    count = await worker.run_taxonomy_load()
    assert count > 80
    async with worker_sessions() as session:
        sql = await session.scalar(select(Skill).where(Skill.slug == "sql"))
    assert sql is not None and "sql" in sql.aliases and sql.name_i18n["ru"] == "SQL"
    # Running again changes nothing and raises nothing.
    assert await worker.run_taxonomy_load() == count
