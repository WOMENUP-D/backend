"""The curated skill catalogue, checked as data.

It is written by hand and will keep growing, so the errors worth catching are
the ones a person makes: two entries claiming the same identifier, a spelling
that would resolve to two different skills, a name missing in one language, a
dimension that no longer exists.
"""

from app.core.constants import ScoreDimension, SkillCategory
from app.seed_skills import CATALOGUE
from app.services import skills


def test_every_slug_is_unique_and_usable_as_an_identifier():
    slugs = [item["slug"] for item in CATALOGUE]
    assert len(slugs) == len(set(slugs))
    for slug in slugs:
        assert slug == skills.slug_for(slug), slug


def test_every_skill_is_named_in_all_three_languages():
    for item in CATALOGUE:
        assert len(item["names"]) == 3, item["slug"]
        assert all(name and name.strip() for name in item["names"]), item["slug"]


def test_categories_and_dimensions_are_part_of_the_vocabulary():
    for item in CATALOGUE:
        assert isinstance(item["category"], SkillCategory), item["slug"]
        for dimension in item["dimensions"]:
            assert isinstance(dimension, ScoreDimension), item["slug"]


def test_no_spelling_points_at_two_different_skills():
    """An alias claimed twice would make matching depend on row order."""
    owner: dict[str, str] = {}
    for item in CATALOGUE:
        uz, ru, en = item["names"]
        for alias in (*item["aliases"], uz, ru, en):
            key = skills.normalise(alias)
            assert key, item["slug"]
            assert owner.setdefault(key, item["slug"]) == item["slug"], key


def test_the_spellings_that_are_already_in_the_database_resolve():
    """Labels the seeded catalogue and demo population actually use.

    Including the two that used to be separate skills because of a dropped
    okina, which is the bug this taxonomy exists to end.
    """
    index = skills.SkillIndex.of(
        [
            type(
                "Row",
                (),
                {
                    "slug": item["slug"],
                    "aliases": [
                        skills.normalise(alias) for alias in (*item["aliases"], *item["names"])
                    ],
                },
            )()
            for item in CATALOGUE
        ]
    )

    assert index.key("jamgʻarma") == index.key("jamgarma") == "savings"
    assert index.key("byudjet") == index.key("budjet") == "budgeting"
    assert index.key("muloqot") == index.key("kommunikatsiya") == "communication"
    assert index.key("1c") == index.key("1С") == "1c"
    assert index.key("buxgalteriya") == index.key("Бухгалтерия") == "accounting"
