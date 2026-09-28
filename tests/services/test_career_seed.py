"""The seeded career directions: grounded in the catalogue, and never a promise.

Most of this runs on the seed data itself, without a database — the six
directions are checked against the courses, tasks, listings, learning paths
and skills the demo dataset holds. A direction that names a skill nothing
teaches *and* nothing asks for, or that ends in a kind of listing the platform
does not carry, would be a direction to nowhere.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.core.constants import OpportunityType, SkillCategory
from app.models.career_path import CareerPath
from app.models.skill import Skill
from app.seed import OPPORTUNITIES, PROGRAMS
from app.seed_careers import CAREERS, load_careers
from app.seed_paths import PATHS
from app.seed_skills import CATALOGUE
from app.seed_tasks import TASKS
from app.services import skills as skill_service


def _index() -> skill_service.SkillIndex:
    """The curated taxonomy, resolved the way `load_skills` writes it."""
    skills = []
    for item in CATALOGUE:
        uz, ru, en = item["names"]
        aliases = {skill_service.normalise(a) for a in (*item["aliases"], uz, ru, en)}
        skills.append(
            Skill(
                slug=item["slug"],
                name_i18n={"uz": uz, "ru": ru, "en": en},
                category=item["category"],
                aliases=sorted(a for a in aliases if a),
            )
        )
    return skill_service.SkillIndex.of(skills)


INDEX = _index()


def _keys(labels) -> set[str]:
    return {INDEX.key(label) for label in labels}


TAUGHT = [_keys(program[9]) for program in PROGRAMS]
PRACTISED = [_keys(task["skills"]) for task in TASKS]
LISTED = [(kind, _keys(skills)) for _, kind, _, _, _, _, skills, _, _ in OPPORTUNITIES]


def test_the_seed_names_six_distinct_directions():
    slugs = [career["slug"] for career in CAREERS]
    assert len(slugs) == 6
    assert len(set(slugs)) == len(slugs)


@pytest.mark.parametrize("career", CAREERS, ids=lambda c: c["slug"])
def test_every_skill_is_a_curated_taxonomy_entry(career):
    curated = {item["slug"] for item in CATALOGUE}
    assert set(career["skills"]) <= curated


@pytest.mark.parametrize("career", CAREERS, ids=lambda c: c["slug"])
def test_every_skill_is_taught_practised_or_asked_for(career):
    for slug in career["skills"]:
        reached = (
            any(slug in keys for keys in TAUGHT)
            or any(slug in keys for keys in PRACTISED)
            or any(slug in keys for _, keys in LISTED)
        )
        assert reached, f"{career['slug']}: nothing on the platform touches {slug}"


@pytest.mark.parametrize("career", CAREERS, ids=lambda c: c["slug"])
def test_every_direction_is_backed_from_both_ends(career):
    wanted = set(career["skills"])
    # Something teaches it...
    assert any(wanted & keys for keys in TAUGHT), career["slug"]
    # ...and a listing of a kind it leads to asks for it.
    kinds = set(career["opportunities"])
    assert any(kind in kinds and wanted & keys for kind, keys in LISTED), career["slug"]


@pytest.mark.parametrize("career", CAREERS, ids=lambda c: c["slug"])
def test_its_learning_path_and_listing_kinds_exist(career):
    if career["learning_path"] is not None:
        assert career["learning_path"] in {path["slug"] for path in PATHS}
    assert career["opportunities"]
    assert all(isinstance(kind, OpportunityType) for kind in career["opportunities"])


@pytest.mark.parametrize("career", CAREERS, ids=lambda c: c["slug"])
def test_it_is_written_in_three_languages_and_promises_no_job(career):
    for field in ("title", "summary", "description"):
        uz, ru, en = career[field]
        assert uz.strip() and ru.strip() and en.strip(), (career["slug"], field)

    uz, ru, en = career["description"]
    # "Can help you prepare for", in each language — never "you will get".
    assert "tayyorlan" in uz
    assert "подготов" in ru
    assert "prepare" in en
    text = " ".join((uz, ru, en)).lower()
    for promise in (
        "kafolat",
        "ishga joylashasiz",
        "гарант",
        "получите работу",
        "guarantee",
        "you will get",
        "will get a job",
    ):
        assert promise not in text, (career["slug"], promise)


# --- loading ----------------------------------------------------------------


async def _taxonomy(session, slugs) -> None:
    for slug in slugs:
        session.add(
            Skill(
                slug=slug,
                name_i18n={"uz": slug},
                category=SkillCategory.PROFESSIONAL,
                aliases=[slug],
                is_curated=True,
            )
        )
    await session.flush()


@pytest.mark.asyncio
async def test_loading_is_idempotent_and_never_mints_a_skill(session):
    every = {slug for career in CAREERS for slug in career["skills"]}
    known = sorted(every)[: len(every) // 2]
    await _taxonomy(session, known)
    skills_before = await session.scalar(select(func.count()).select_from(Skill))

    first = await load_careers(session)
    second = await load_careers(session)

    assert first["careers"] > 0
    assert second["careers"] == 0
    assert second["updated"] == first["careers"]
    assert await session.scalar(select(func.count()).select_from(Skill)) == skills_before
    for career in (await session.execute(select(CareerPath))).scalars():
        # Only skills the taxonomy knows are written; the rest are skipped.
        assert set(career.skill_slugs) <= set(known)
        assert career.skill_slugs


@pytest.mark.asyncio
async def test_a_direction_with_no_known_skill_is_not_published(session):
    await _taxonomy(session, [f"boshqa-{uuid.uuid4().hex[:6]}"])

    counts = await load_careers(session)

    assert counts["careers"] == 0
    assert counts["skipped"] == len(CAREERS)
    assert await session.scalar(select(func.count()).select_from(CareerPath)) == 0
