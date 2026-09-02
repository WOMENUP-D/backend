"""The programme catalogue is public; unpublished drafts are not.

The landing page advertises the catalogue to visitors, so the list and the card
are readable without an account. That makes the draft check load-bearing: before
it, `GET /programs/{id}` returned any programme to any signed-in caller, and
opening the endpoint would have widened that to everyone.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    Language,
    OpportunitySource,
    OpportunityType,
    ProgramCategory,
    ProgramFormat,
)
from app.models.opportunity import Opportunity
from app.models.program import Program


async def _make_program(session: AsyncSession, *, published: bool) -> Program:
    program = Program(
        slug=f"test-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": "Test", "ru": "Тест", "en": "Test"},
        goal_i18n={"uz": "Maqsad"},
        description_i18n={"uz": "Tavsif"},
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        duration_hours=10,
        duration_weeks=2,
        learning_outcomes=[{"uz": "a"}, {"uz": "b"}, {"uz": "c"}],
        skills_taught=["test"],
        is_published=published,
    )
    session.add(program)
    await session.flush()
    return program


@pytest.mark.asyncio
async def test_catalogue_is_readable_without_an_account(client, session):
    await _make_program(session, published=True)
    response = await client.get("/api/v1/programs")
    assert response.status_code == 200
    assert response.json()["total"] >= 1


@pytest.mark.asyncio
async def test_published_card_is_readable_without_an_account(client, session):
    program = await _make_program(session, published=True)
    response = await client.get(f"/api/v1/programs/{program.id}")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_draft_is_hidden_from_visitors(client, session):
    program = await _make_program(session, published=False)
    response = await client.get(f"/api/v1/programs/{program.id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_draft_is_hidden_from_an_ordinary_signed_in_user(client, session, auth_headers):
    """The gap this closes: being logged in was never authority to read drafts."""
    program = await _make_program(session, published=False)
    response = await client.get(f"/api/v1/programs/{program.id}", headers=auth_headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_draft_is_visible_to_staff(client, session, admin_headers):
    program = await _make_program(session, published=False)
    response = await client.get(f"/api/v1/programs/{program.id}", headers=admin_headers)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_drafts_never_appear_in_the_catalogue_listing(client, session):
    draft = await _make_program(session, published=False)
    response = await client.get("/api/v1/programs")
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert str(draft.id) not in ids


@pytest.mark.asyncio
async def test_enrolling_still_requires_an_account(client, session):
    program = await _make_program(session, published=True)
    response = await client.post(f"/api/v1/programs/{program.id}/enroll")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_opportunity_listing_is_public(client):
    """A vacancy with a salary is the strongest thing to show a visitor."""
    response = await client.get("/api/v1/opportunities")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_opportunity_stats_are_public(client, session):
    """The landing page states the size of the offer before asking to sign up."""
    session.add(
        Opportunity(
            source=OpportunitySource.EDU_JOB,
            type=OpportunityType.VACANCY,
            title_i18n={"uz": "Test"},
            required_skills=[],
            reward={},
            region="tashkent_city",
            is_active=True,
        )
    )
    await session.flush()

    response = await client.get("/api/v1/opportunities/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    # Enum *values*, matching what the listing returns — not "OpportunityType.VACANCY".
    assert body["by_type"]["vacancy"] >= 1
    assert body["by_source"]["edu_job"] >= 1
    assert body["regions"] >= 1


@pytest.mark.asyncio
async def test_opportunity_stats_count_only_what_the_listing_shows(client, session):
    """A number on the front page that the catalogue cannot back up is a lie."""
    session.add(
        Opportunity(
            source=OpportunitySource.EDU_JOB,
            type=OpportunityType.VACANCY,
            title_i18n={"uz": "Retired"},
            required_skills=[],
            reward={},
            is_active=False,
        )
    )
    await session.flush()

    stats = (await client.get("/api/v1/opportunities/stats")).json()
    listing = (await client.get("/api/v1/opportunities?size=100")).json()
    assert stats["total"] == listing["total"]


@pytest.mark.asyncio
async def test_personal_opportunity_routes_stay_private(client):
    """Matching, applying and consent are personal and must not open up."""
    for path in (
        "/api/v1/opportunities/recommended",
        "/api/v1/opportunities/me/applications",
        "/api/v1/opportunities/consent-status",
    ):
        assert (await client.get(path)).status_code == 401
