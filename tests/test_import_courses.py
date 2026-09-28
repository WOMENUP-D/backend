"""Loading somebody else's courses into the catalogue.

Two things must hold. A course that is not ours has to arrive marked as not
ours — an address and a source — because that is the only thing standing
between "a course we list" and "a course we run". And an import must never be
able to damage the portal's own programmes: it runs unattended, from a file,
against a live catalogue.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.core.constants import Language, ProgramCategory
from app.import_courses import ImportError_, clean, load, main, slug_for
from app.models.program import Program

COURSE = {
    "source": "stepik",
    "external_id": 63054,
    "title": "Основы статистики",
    "url": "https://stepik.org/course/76",
    "category": "digital_safety",
    "language": "ru",
    "provider": "Stepik",
    "summary": "Курс о том, как описывать данные.",
}


# ------------------------------------------------------------ the file


def test_a_course_becomes_a_row_the_table_accepts():
    row = clean(COURSE, 0)
    assert row["slug"] == "stepik-63054"
    assert row["category"] is ProgramCategory.DIGITAL_SAFETY
    assert row["language"] is Language.RU
    assert row["external_url"] == COURSE["url"]
    assert row["source"] == "stepik"
    assert row["is_published"] is True
    # The name stays in the language it is taught in: translating it would
    # invent a course that does not exist under that name.
    assert set(row["title_i18n"].values()) == {"Основы статистики"}


@pytest.mark.parametrize(
    "change",
    [
        {"url": "http://stepik.org/course/76"},  # not encrypted
        {"url": "stepik.org/course/76"},  # not an address
        {"category": "astrology"},  # not one of the twelve
        {"title": "   "},
        {"source": ""},
        {"external_id": ""},
    ],
)
def test_a_record_that_would_mislead_is_refused(change):
    with pytest.raises(ImportError_):
        clean({**COURSE, **change}, 3)


def test_the_slug_is_stable_so_a_second_run_finds_the_same_row():
    assert slug_for("stepik", "63054") == slug_for("Stepik", "63054")
    assert slug_for("ochiq kurs", "python/asoslari") == "ochiq-kurs-python-asoslari"


# ------------------------------------------------------------ the catalogue


@pytest.mark.asyncio
async def test_courses_are_added_then_updated_rather_than_duplicated(session):
    added, updated = await load(session, [clean(COURSE, 0)])
    assert (added, updated) == (1, 0)

    renamed = clean({**COURSE, "title": "Основы статистики. Часть 1"}, 0)
    added, updated = await load(session, [renamed])
    assert (added, updated) == (0, 1)

    rows = (await session.scalars(select(Program).where(Program.source == "stepik"))).all()
    assert len(rows) == 1
    assert rows[0].title_i18n["ru"] == "Основы статистики. Часть 1"


@pytest.mark.asyncio
async def test_an_import_never_overwrites_a_programme_of_ours(session):
    ours = Program(
        slug="stepik-63054",  # the same slug, on purpose
        title_i18n={"uz": "Bizning dastur"},
        category=ProgramCategory.HEALTH,
        provider="WomanUP",
    )
    session.add(ours)
    await session.flush()

    added, updated = await load(session, [clean(COURSE, 0)])
    assert (added, updated) == (0, 0)

    await session.refresh(ours)
    assert ours.title_i18n == {"uz": "Bizning dastur"}
    assert ours.category is ProgramCategory.HEALTH
    assert ours.external_url is None


@pytest.mark.asyncio
async def test_a_course_of_ours_is_told_apart_by_its_fields(session):
    await load(session, [clean(COURSE, 0)])
    imported = await session.scalar(select(Program).where(Program.slug == "stepik-63054"))
    assert imported.external_url and imported.source
    # Nothing about it claims to be ours.
    assert imported.author_id is None
    assert imported.has_certificate is False
    assert imported.learning_outcomes == []


# ------------------------------------------------------------ the command


@pytest.mark.asyncio
async def test_a_broken_file_changes_nothing(tmp_path, capsys):
    path = tmp_path / "courses.json"
    path.write_text(json.dumps([COURSE, {**COURSE, "category": "astrology"}]), encoding="utf-8")
    assert await main([str(path)]) == 2
    assert "Refused" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_two_records_for_the_same_course_are_refused(tmp_path, capsys):
    path = tmp_path / "courses.json"
    path.write_text(json.dumps([COURSE, {**COURSE, "title": "Другое имя"}]), encoding="utf-8")
    assert await main([str(path)]) == 2
    assert "slug" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_dry_run_reads_the_file_and_writes_nothing(tmp_path, capsys):
    path = tmp_path / "courses.json"
    path.write_text(json.dumps([COURSE]), encoding="utf-8")
    assert await main([str(path), "--dry-run"]) == 0
    assert "Nothing written" in capsys.readouterr().out


def test_the_shipped_file_is_one_the_importer_accepts():
    """The 794 courses in the repository, checked without a database."""
    from pathlib import Path

    records = json.loads(Path("data/partner_courses.json").read_text(encoding="utf-8"))
    assert len(records) > 500
    rows = [clean(record, index) for index, record in enumerate(records)]
    assert len({row["slug"] for row in rows}) == len(rows), "two courses share a slug"
    assert all(row["external_url"].startswith("https://") for row in rows)
    assert {row["source"] for row in rows} == {"stepik"}
