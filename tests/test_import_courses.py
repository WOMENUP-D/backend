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
    # Statistics is a digital skill, not digital safety (QA audit G-04).
    assert row["category"] is ProgramCategory.DIGITAL_SKILLS
    assert row["language"] is Language.RU
    assert row["external_url"] == COURSE["url"]
    assert row["source"] == "stepik"
    assert row["is_published"] is True
    # The name stays in the language it is taught in: translating it would
    # invent a course that does not exist under that name.
    # Filed under its own language only, so the Uzbek converter never sees it.
    assert row["title_i18n"] == {"ru": "Основы статистики"}
    assert row["description_i18n"] == {"ru": COURSE["summary"]}
    # The platform does not say video or text: nothing is claimed.
    assert row["format"] is None


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
    """The courses in the repository, checked without a database."""
    from pathlib import Path

    records = json.loads(Path("data/partner_courses.json").read_text(encoding="utf-8"))
    assert len(records) > 500
    rows = [clean(record, index) for index, record in enumerate(records)]
    assert len({row["slug"] for row in rows}) == len(rows), "two courses share a slug"
    assert all(row["external_url"].startswith("https://") for row in rows)
    # Stepik is the bulk of it; the Uzbek platforms are the reason the file
    # exists at all for a reader who does not read Russian; the English
    # courses (Coursera, Khan Academy) used to live in a separate block on the
    # programmes page and now sit in the same catalogue as everything else.
    sources = {row["source"] for row in rows}
    assert {"stepik", "ochiqkurs", "coursera"} <= sources
    assert sources <= {"stepik", "ochiqkurs", "direktor", "spbu", "coursera", "khanacademy"}
    assert sum(row["source"] == "coursera" for row in rows) == 14
    assert sum(row["source"] == "khanacademy" for row in rows) == 1


# ------------------------------------------------------------ the rules (QA audit)


@pytest.mark.parametrize(
    ("title", "filed", "expected"),
    [
        ("Поколение Python", "digital_safety", ProgramCategory.DIGITAL_SKILLS),
        ("Основы кибербезопасности", "digital_safety", ProgramCategory.DIGITAL_SAFETY),
        ("Защита персональных данных - базовый курс", "digital_safety", "digital_safety"),
        ("«Профессия — Белый Хакер»", "vocational_skills", ProgramCategory.DIGITAL_SAFETY),
        ("Как открыть заведение общественного питания", "health", "entrepreneurship"),
        ("Юридическая психология", "health", ProgramCategory.LEGAL_LITERACY),
        ("Python для школьников", "digital_safety", ProgramCategory.DIGITAL_SKILLS),
        ("Психология", "health", ProgramCategory.HEALTH),
    ],
)
def test_the_category_follows_the_title(title, filed, expected):
    row = clean({**COURSE, "title": title, "category": filed}, 0)
    assert row["category"] == ProgramCategory(expected)


def test_a_course_without_skills_is_tagged_from_its_title_only():
    row = clean({**COURSE, "title": "Основы программирования на языке C#", "skills": []}, 0)
    assert row["skills_taught"] == ["programming"]
    # The summary is not mined: mentioning a language does not teach it.
    plain = clean(
        {**COURSE, "title": "Олимпиадные задачи", "summary": "На русском языке", "skills": []}, 0
    )
    assert plain["skills_taught"] == []


def test_the_file_s_own_skills_win_over_the_title():
    row = clean({**COURSE, "title": "Excel и Python", "skills": ["excel"]}, 0)
    assert row["skills_taught"] == ["excel"]


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Подготовка к ЕГЭ по математике"},
        {"title": "Курс по машинному обучению (архивный курс 2022)"},
        {"title": "Программирование на С++ (дорабатывается)"},
        {"available": False, "unavailable_reason": "private"},
    ],
)
def test_a_course_not_on_offer_is_imported_unpublished(change):
    assert clean({**COURSE, **change}, 0)["is_published"] is False


def test_vlookup_is_not_mistaken_for_an_exam():
    assert clean({**COURSE, "title": "Функция ВПР в Excel"}, 0)["is_published"] is True


@pytest.mark.asyncio
async def test_an_imported_course_gets_a_published_at_for_a_stable_order(session):
    await load(session, [clean(COURSE, 0)])
    row = await session.scalar(select(Program).where(Program.slug == "stepik-63054"))
    assert row.published_at is not None


def test_the_shipped_file_hides_the_courses_the_platform_does_not_offer():
    from pathlib import Path

    records = json.loads(Path("data/partner_courses.json").read_text(encoding="utf-8"))
    rows = {str(r["external_id"]): clean(r, i) for i, r in enumerate(records)}
    for private in ("102002", "121172", "213818"):
        assert rows[private]["is_published"] is False
    categories = [row["category"] for row in rows.values()]
    assert categories.count(ProgramCategory.DIGITAL_SAFETY) < 20
