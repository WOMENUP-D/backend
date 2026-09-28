"""Finding a course, and being offered one you can read.

Three quarters of the catalogue arrived from other platforms with a title, a
description and nothing else. That shaped both of these: a search that only
read titles found two of the forty-seven health courses, and a recommender
that ranks on skills had nothing to rank imported courses by. The language
matters for the same reason — 740 of the 804 are in Russian.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.constants import Language, ProgramCategory
from app.import_courses import clean
from app.models.program import Program
from app.services.recommendation import language_distance


def _course(**overrides) -> Program:
    """A published course, external unless told otherwise."""
    base = {
        "slug": "stepik-1",
        "title_i18n": {"uz": "Kurs", "ru": "Курс", "en": "Course"},
        "description_i18n": {},
        "category": ProgramCategory.DIGITAL_SAFETY,
        "language": Language.RU,
        "is_published": True,
        "external_url": "https://stepik.org/course/1",
        "source": "stepik",
    }
    return Program(**{**base, **overrides})


# ------------------------------------------------------------ the search


@pytest.mark.asyncio
async def test_a_course_is_found_by_its_description(client: AsyncClient, session):
    """The only words most imported courses have are in the description."""
    session.add(
        _course(
            title_i18n={"uz": "Vital Signs", "ru": "Vital Signs", "en": "Vital Signs"},
            description_i18n={
                "uz": "Что тело говорит о здоровье",
                "ru": "Что тело говорит о здоровье",
                "en": "What the body says about health",
            },
            category=ProgramCategory.HEALTH,
        )
    )
    await session.flush()

    found = await client.get("/api/v1/programs", params={"search": "здоровье"})
    assert found.status_code == 200
    assert [item["slug"] for item in found.json()["items"]] == ["stepik-1"]


@pytest.mark.asyncio
async def test_the_search_still_matches_a_title(client: AsyncClient, session):
    session.add(_course(title_i18n={"uz": "Python", "ru": "Python", "en": "Python"}))
    await session.flush()
    found = await client.get("/api/v1/programs", params={"search": "python"})
    assert found.json()["total"] == 1


@pytest.mark.asyncio
async def test_a_word_in_neither_finds_nothing(client: AsyncClient, session):
    session.add(_course())
    await session.flush()
    found = await client.get("/api/v1/programs", params={"search": "астрология"})
    assert found.json()["total"] == 0


# ------------------------------------------------------------ the language


def test_her_own_language_comes_first():
    uz = _course(language=Language.UZ)
    ru = _course(language=Language.RU)
    en = _course(language=Language.EN)
    assert language_distance(uz, Language.UZ) == 0
    # Russian is the second language most of this audience reads, and most of
    # the catalogue is in it — below her own, never hidden.
    assert language_distance(ru, Language.UZ) == 1
    assert language_distance(en, Language.UZ) == 2


def test_a_reader_without_a_language_is_not_narrowed():
    assert language_distance(_course(language=Language.EN), None) == 0


def test_a_russian_reader_is_offered_russian_first():
    assert language_distance(_course(language=Language.RU), Language.RU) == 0
    assert language_distance(_course(language=Language.UZ), Language.RU) == 1
    assert language_distance(_course(language=Language.EN), Language.RU) == 2


# ------------------------------------------------------------ the skills


def test_an_imported_course_carries_the_skills_it_names():
    row = clean(
        {
            "source": "stepik",
            "external_id": 67,
            "title": "Программирование на Python",
            "url": "https://stepik.org/course/67",
            "category": "digital_safety",
            "language": "ru",
            "skills": ["python", " ", "алгоритмы"],
        },
        0,
    )
    # Blank entries are dropped; the rest arrive as written.
    assert row["skills_taught"] == ["python", "алгоритмы"]


def test_a_course_that_names_none_gets_none():
    row = clean(
        {
            "source": "stepik",
            "external_id": 1,
            "title": "Курс",
            "url": "https://stepik.org/course/1",
            "category": "health",
        },
        0,
    )
    assert row["skills_taught"] == []


def test_the_shipped_file_carries_skills_and_uzbek_courses():
    """What is actually in the repository, checked without a database."""
    import json
    from pathlib import Path

    records = json.loads(Path("data/partner_courses.json").read_text(encoding="utf-8"))
    with_skills = [r for r in records if r.get("skills")]
    uzbek = [r for r in records if r.get("language") == "uz"]

    assert len(records) > 800
    # Not all of them: a course whose title names no skill keeps an empty list
    # rather than a guessed one.
    assert len(with_skills) / len(records) > 0.6
    assert len(uzbek) >= 10
    assert all(r["url"].startswith("https://") for r in uzbek)
