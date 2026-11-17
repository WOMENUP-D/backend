"""The directions file, held to the catalogue it promises courses from.

No database: the partner catalogue is read through the importer's own
`clean`, which is exactly what the worker writes into `programs`, so the
coverage counted here is the coverage production gets from the same file.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import import_courses
from app.core.constants import ProficiencyLevel, ProgramCategory
from app.services import learning_directions as ld
from app.services.uzbek import to_new_alphabet

ROOT = Path(__file__).resolve().parents[2]
FILE = ROOT / "data" / "learning_directions.json"


@pytest.fixture(scope="module")
def catalogue() -> ld.DirectionCatalogue:
    return ld.parse(json.loads(FILE.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def partner_courses() -> list[SimpleNamespace]:
    records = json.loads((ROOT / "data" / "partner_courses.json").read_text(encoding="utf-8"))
    rows = [import_courses.clean(record, i) for i, record in enumerate(records)]
    return [
        SimpleNamespace(id=uuid.UUID(int=i), level=None, **row)
        for i, row in enumerate(rows)
        if row["is_published"]
    ]


def _course(title: str, tags: list[str], **extra) -> SimpleNamespace:
    fields = {
        "id": uuid.uuid4(),
        "title_i18n": {"ru": title},
        "skills_taught": tags,
        "category": ProgramCategory.DIGITAL_SKILLS,
        "level": None,
        "language": "ru",
        "is_published": True,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def test_every_direction_has_at_least_three_published_partner_courses(catalogue, partner_courses):
    counts = ld.coverage(catalogue, partner_courses)
    thin = {slug: n for slug, n in counts.items() if n < catalogue.min_courses}
    assert not thin, f"directions the catalogue cannot serve: {thin}"


def test_every_interest_but_other_leads_somewhere(catalogue):
    bank = json.loads((ROOT / "data" / "diagnostic_v4.json").read_text(encoding="utf-8"))
    q6 = next(q for q in bank["questions"] if q.get("code") == "pro.q6")
    options = {o["id"]: o["label"] for o in q6["options"]}
    for interest in options:
        if interest == "other":
            continue
        assert catalogue.for_interest(interest), interest
    # The words shown for her interest are the diagnostic's own.
    for interest, label in catalogue.interests.items():
        assert options[interest] == label
    for direction in catalogue.directions:
        assert set(direction.interests) <= set(options) - {"other"}, direction.slug
        assert direction.interests or direction.dimensions, direction.slug


def test_words_are_in_three_languages_and_the_reformed_alphabet(catalogue):
    for direction in catalogue.directions:
        for words in (direction.title_i18n, direction.summary_i18n):
            assert set(words) == {"uz", "ru", "en"}, direction.slug
            assert all(value.strip() for value in words.values()), direction.slug
            assert to_new_alphabet(words["uz"]) == words["uz"], words["uz"]


def test_a_topic_that_is_a_skill_brings_every_name_of_it():
    assert {"english", "ingliz tili", "английский язык"} <= ld.topic_labels("english")
    # An alias of a broader skill stays itself: Python is not all programming.
    assert ld.topic_labels("python") == {"python"}


def test_stated_level_orders_first_then_the_title(catalogue):
    direction = next(d for d in catalogue.directions if d.slug == "python")
    advanced = _course("Python", ["python"], level=ProficiencyLevel.ADVANCED)
    titled_first = _course("Python для начинающих", ["python"])
    unknown = _course("Python", ["python"])
    stated_first = _course("Python", ["python"], level=ProficiencyLevel.BEGINNER)
    interviews = _course("Python для собеседования", ["python"])

    order = ld.ordered(direction, [advanced, interviews, unknown, titled_first, stated_first])

    assert order[0] in (titled_first, stated_first) and order[1] in (titled_first, stated_first)
    assert order[2] is unknown
    assert {id(c) for c in order[3:]} == {id(advanced), id(interviews)}


def test_a_course_is_listed_once_and_only_when_it_belongs(catalogue):
    direction = next(d for d in catalogue.directions if d.slug == "ui-ux-design")
    figma = _course("Основы Figma", ["дизайн"])
    flowers = _course("Фитодизайн интерьерных пространств", ["дизайн"])
    order = ld.ordered(direction, [figma, figma, flowers])
    assert order == [figma]


def test_the_language_she_reads_breaks_ties(catalogue):
    direction = next(d for d in catalogue.directions if d.slug == "python")
    russian = _course("A Python", ["python"], language="ru")
    uzbek = _course("B Python", ["python"], language="uz")
    assert ld.ordered(direction, [russian, uzbek], language="uz") == [uzbek, russian]
    assert ld.ordered(direction, [russian, uzbek], language="ru") == [russian, uzbek]


def test_a_broken_file_degrades_to_nothing(tmp_path):
    broken = tmp_path / "directions.json"
    broken.write_text('{"directions": [{"slug": "x", "dimensions": ["nope"]}]}')
    assert ld.load(broken) is None
    assert ld.load(tmp_path / "missing.json") is None
