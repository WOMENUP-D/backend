"""The catalogue search and its ordering (QA audit B-07, B-08, B-24, G-09)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.constants import Language, ProgramCategory, SkillCategory
from app.models.program import Program
from app.models.skill import Skill
from app.services import catalogue_search


def _course(slug: str, title: str, **extra) -> Program:
    return Program(
        slug=slug,
        title_i18n={"ru": title},
        category=ProgramCategory.DIGITAL_SKILLS,
        language=extra.pop("language", Language.RU),
        is_published=True,
        **extra,
    )


def test_spellings_cover_both_alphabets_and_yo():
    assert {"tikuvçilik", "tikuvchilik"} <= catalogue_search.spellings("Tikuvçilik")
    assert catalogue_search.spellings("ребёнок") == {"ребенок"}


def test_uzbek_endings_come_off_before_the_lookup():
    assert "salomatlik" in catalogue_search.stems("salomatligi")
    assert "kurs" in catalogue_search.stems("kurslari")


async def _search(client: AsyncClient, term: str) -> list[str]:
    found = await client.get("/api/v1/programs", params={"search": term, "size": 100})
    assert found.status_code == 200
    return sorted(item["slug"] for item in found.json()["items"])


@pytest.mark.asyncio
async def test_wildcards_are_characters_not_wildcards(client, session):
    session.add_all([_course("a", "Python"), _course("b", "Скидка 50% на Excel")])
    await session.flush()
    assert await _search(client, "%%") == []
    assert await _search(client, "__") == []
    assert await _search(client, "50%") == ["b"]


@pytest.mark.asyncio
async def test_yo_and_ye_are_one_letter(client, session):
    session.add_all([_course("a", "Ваш ребёнок"), _course("b", "Ребенок и школа")])
    await session.flush()
    assert await _search(client, "ребенок") == ["a", "b"]
    assert await _search(client, "ребёнок") == ["a", "b"]


@pytest.mark.asyncio
async def test_a_short_term_matches_whole_words_and_one_letter_is_no_search(client, session):
    session.add_all([_course("a", "Карьера в IT"), _course("b", "Digital marketing")])
    await session.flush()
    assert await _search(client, "IT") == ["a"]
    # One character filters nothing rather than finding every "a".
    assert await _search(client, "a") == ["a", "b"]


@pytest.mark.asyncio
async def test_an_uzbek_term_finds_the_russian_courses_through_the_taxonomy(client, session):
    session.add(
        Skill(
            slug="english",
            name_i18n={"uz": "Ingliz tili", "ru": "Английский язык", "en": "English"},
            category=SkillCategory.LANGUAGE,
            dimensions=["leadership"],
            aliases=["ingliz tili", "английский", "english", "английский язык"],
            is_curated=True,
            is_active=True,
        )
    )
    session.add_all([_course("a", "Английский для начинающих"), _course("b", "Python")])
    await session.flush()
    assert await _search(client, "ingliz tili") == ["a"]
    assert await _search(client, "Ingliz tilini") == ["a"]


@pytest.mark.asyncio
async def test_an_alias_of_a_broader_skill_does_not_widen_the_search(client, session):
    session.add(
        Skill(
            slug="programming",
            name_i18n={"uz": "Dasturlash", "ru": "Программирование", "en": "Programming"},
            category=SkillCategory.DIGITAL,
            dimensions=["digital_skills"],
            aliases=["dasturlash", "программирование", "python"],
            is_curated=True,
            is_active=True,
        )
    )
    session.add_all([_course("a", "Python"), _course("b", "Программирование на Java")])
    await session.flush()
    assert await _search(client, "python") == ["a"]


@pytest.mark.asyncio
async def test_pagination_returns_every_course_once(client, session):
    # Same published_at for all: only the id tie-breaker keeps the order stable.
    session.add_all([_course(f"c{i:02d}", f"Курс {i}") for i in range(30)])
    await session.flush()
    seen: list[str] = []
    for page in (1, 2, 3, 4):
        found = await client.get("/api/v1/programs", params={"page": page, "size": 8})
        seen += [item["slug"] for item in found.json()["items"]]
    assert len(seen) == 30 and len(set(seen)) == 30


@pytest.mark.asyncio
async def test_her_language_can_come_first_without_hiding_the_rest(client, session):
    session.add_all(
        [
            _course("ru1", "Курс"),
            _course("uz1", "Kurs", language=Language.UZ),
            _course("en1", "Course", language=Language.EN),
        ]
    )
    await session.flush()
    found = await client.get("/api/v1/programs", params={"prefer_language": "uz"})
    items = found.json()["items"]
    assert items[0]["slug"] == "uz1"
    assert len(items) == 3
    assert all("language" in item for item in items)


@pytest.mark.asyncio
async def test_the_catalogue_sorts_by_title_or_newest(client, session):
    """QA audit B-25: a sort the list offers, with the id still breaking ties."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    session.add_all(
        [
            _course("b", "Bета", published_at=now - timedelta(days=2)),
            _course("a", "Альфа", published_at=now - timedelta(days=1)),
            _course("c", "Гамма", published_at=now),
        ]
    )
    await session.flush()
    by_title = await client.get("/api/v1/programs", params={"sort": "title"})
    assert [i["slug"] for i in by_title.json()["items"]] == ["b", "a", "c"]
    newest = await client.get("/api/v1/programs", params={"sort": "newest"})
    assert [i["slug"] for i in newest.json()["items"]] == ["c", "a", "b"]
    bad = await client.get("/api/v1/programs", params={"sort": "price"})
    assert bad.status_code == 422
