"""The skills endpoints: a public vocabulary, and a private profile.

The catalogue is open — it is the words courses and listings are written in.
Everything about a particular woman is hers alone: `/skills/me` answers for the
caller and for nobody else, and no route here exposes one woman's skills to
another account.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.constants import (
    EnrollmentStatus,
    Language,
    ProgramCategory,
    ProgramFormat,
    Role,
    SkillCategory,
)
from app.core.security import create_token
from app.models.audit import AuditLog
from app.models.profile import Profile
from app.models.program import Enrollment, Program, ProgramModule
from app.models.skill import Skill
from app.models.user import User
from app.services import skills as skill_service

CATALOGUE = "/api/v1/skills"
MINE = "/api/v1/skills/me"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _editor(session) -> dict[str, str]:
    """An admin with a row of her own.

    Curating the vocabulary is audited, and `audit_logs.actor_id` is a foreign
    key — a token minted for an id that was never inserted fails on the write,
    not on the request.
    """
    admin = User(phone=_phone())
    session.add(admin)
    await session.flush()
    token = create_token(admin.id, Role.ADMIN, "access", {"roles": [Role.ADMIN.value]})
    return {"Authorization": f"Bearer {token}"}


async def _skill(
    session,
    slug="accounting",
    *,
    curated=True,
    aliases=("buxgalteriya", "бухгалтерия"),
) -> Skill:
    skill = Skill(
        slug=slug,
        name_i18n={"uz": "Buxgalteriya", "ru": "Бухгалтерия", "en": "Accounting"},
        category=SkillCategory.PROFESSIONAL,
        dimensions=["employment"],
        aliases=[skill_service.normalise(alias) for alias in aliases],
        is_curated=curated,
    )
    session.add(skill)
    await session.flush()
    return skill


async def _program(session, *, taught: list[str], certificate: bool = False) -> Program:
    program = Program(
        slug=f"course-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Kurs", "ru": "Курс", "en": "Course"},
        goal_i18n={},
        description_i18n={},
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        learning_outcomes=[],
        skills_taught=taught,
        has_certificate=certificate,
        is_published=True,
    )
    session.add(program)
    await session.flush()
    return program


@pytest.mark.asyncio
async def test_the_vocabulary_is_public(client, session):
    await _skill(session)

    response = await client.get(CATALOGUE)

    assert response.status_code == 200
    assert response.json()["items"][0]["slug"] == "accounting"


@pytest.mark.asyncio
async def test_uncurated_entries_stay_out_of_the_public_list(client, session):
    await _skill(session, "macrame", curated=False, aliases=("macrame",))

    listed = await client.get(CATALOGUE)
    everything = await client.get(f"{CATALOGUE}?curated_only=false")

    assert listed.json()["total"] == 0
    assert everything.json()["total"] == 1


@pytest.mark.asyncio
async def test_search_finds_a_skill_by_any_of_its_names_or_spellings(client, session):
    await _skill(session)

    for query in ("Бухгалтер", "accounting", "buxgalteriya"):
        response = await client.get(f"{CATALOGUE}?search={query}")
        assert response.json()["total"] == 1, query


@pytest.mark.asyncio
async def test_an_unknown_skill_is_a_404(client, session):
    assert (await client.get(f"{CATALOGUE}/nothing-like-this")).status_code == 404


@pytest.mark.asyncio
async def test_only_content_roles_may_extend_the_vocabulary(client, session, auth_headers):
    payload = {"slug": "welding", "name_i18n": {"uz": "Payvandlash", "en": "Welding"}}

    assert (await client.post(CATALOGUE, json=payload, headers=auth_headers)).status_code == 403
    assert (await client.post(CATALOGUE, json=payload)).status_code == 401


@pytest.mark.asyncio
async def test_an_admin_adds_a_skill_and_it_is_audited(client, session):
    editor = await _editor(session)
    payload = {
        "slug": "welding",
        "name_i18n": {"uz": "Payvandlash", "ru": "Сварка", "en": "Welding"},
        "category": "craft",
        "dimensions": ["employment"],
        "aliases": ["payvandlash"],
    }

    created = await client.post(CATALOGUE, json=payload, headers=editor)

    assert created.status_code == 201
    # The names count as spellings, each stored as written rather than
    # transliterated, so a label in any language resolves to this skill.
    assert set(created.json()["aliases"]) >= {"payvandlash", "сварка", "welding"}
    assert (await client.post(CATALOGUE, json=payload, headers=editor)).status_code == 409
    audited = await session.execute(select(AuditLog).where(AuditLog.action == "skill.create"))
    assert len(list(audited.scalars())) == 1


@pytest.mark.asyncio
async def test_editing_an_uncurated_entry_names_it_properly(client, session):
    editor = await _editor(session)
    await _skill(session, "macrame", curated=False, aliases=("macrame",))

    response = await client.patch(
        f"{CATALOGUE}/macrame",
        json={"name_i18n": {"uz": "Makrame", "ru": "Макраме", "en": "Macramé"}},
        headers=editor,
    )

    assert response.status_code == 200
    assert response.json()["is_curated"] is True
    assert response.json()["name_i18n"]["ru"] == "Макраме"


@pytest.mark.asyncio
async def test_her_skills_need_an_account(app_client):
    assert (await app_client.get(MINE)).status_code == 401


@pytest.mark.asyncio
async def test_her_skills_are_only_ever_hers(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    stranger = User(phone=_phone())
    session.add(stranger)
    await session.flush()
    await _skill(session)
    await _skill(session, "sewing", aliases=("tikuvchilik",))
    await skill_service.sync_self_reported(session, user_id, ["buxgalteriya"])
    await skill_service.sync_self_reported(session, stranger.id, ["tikuvchilik"])

    body = (await client.get(MINE, headers=auth_headers)).json()

    assert [item["skill"]["slug"] for item in body["skills"]] == ["accounting"]


@pytest.mark.asyncio
async def test_finishing_a_course_puts_a_learned_skill_on_her_profile(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    await _skill(session)
    program = await _program(session, taught=["buxgalteriya"], certificate=True)
    module = ProgramModule(program_id=program.id, order_index=0, title_i18n={"uz": "Modul"})
    session.add(module)
    enrollment = Enrollment(
        user_id=user_id, program_id=program.id, status=EnrollmentStatus.IN_PROGRESS
    )
    session.add(enrollment)
    await session.flush()

    done = await client.post(
        f"/api/v1/programs/enrollments/{enrollment.id}/progress",
        json={"module_id": str(module.id), "completed": True},
        headers=auth_headers,
    )
    assert done.status_code == 200

    body = (await client.get(MINE, headers=auth_headers)).json()
    [accounting] = body["skills"]
    assert accounting["skill"]["slug"] == "accounting"
    assert accounting["skill"]["name_i18n"]["ru"] == "Бухгалтерия"
    # Taught and certified, but nobody has checked what she can do with it.
    assert accounting["status"] == "learned"
    assert {item["kind"] for item in accounting["evidence"]} == {
        "course_completion",
        "certificate",
    }


@pytest.mark.asyncio
async def test_saving_the_profile_records_what_she_says_about_herself(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    session.add(Profile(user_id=user_id, skills=[]))
    await _skill(session)
    await session.flush()

    saved = await client.put(
        "/api/v1/users/me/profile",
        json={"skills": ["Бухгалтерия"], "news_interests": []},
        headers=auth_headers,
    )
    assert saved.status_code == 200

    body = (await client.get(MINE, headers=auth_headers)).json()
    [accounting] = body["skills"]
    assert accounting["status"] == "self_reported"
    # Written in Russian, resolved to the one skill — not minted as a new one.
    assert accounting["skill"]["slug"] == "accounting"


@pytest.mark.asyncio
async def test_skills_to_improve_come_from_what_the_platform_offers(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    await _skill(session, "sewing", aliases=("tikuvchilik",))
    await _program(session, taught=["tikuvchilik"])
    await session.flush()

    body = (await client.get(MINE, headers=auth_headers)).json()

    [gap] = body["improve"]
    assert gap["skill"]["slug"] == "sewing"
    assert gap["programs"] == 1
    assert gap["program_title_i18n"]["en"] == "Course"
