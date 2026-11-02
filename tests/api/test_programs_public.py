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


async def _tagged(
    session: AsyncSession,
    *,
    skills: list[str],
    category=ProgramCategory.VOCATIONAL_SKILLS,
    hours: int | None = 10,
    level=None,
    title: str = "Test",
    published: bool = True,
) -> Program:
    program = Program(
        slug=f"test-{uuid.uuid4().hex[:8]}",
        title_i18n={"uz": title, "ru": title, "en": title},
        goal_i18n={"uz": "Maqsad"},
        description_i18n={"uz": "Tavsif"},
        category=category,
        format=ProgramFormat.VIDEO,
        language=Language.UZ,
        duration_hours=hours,
        duration_weeks=2 if hours else None,
        level=level,
        learning_outcomes=[{"uz": "a"}, {"uz": "b"}, {"uz": "c"}],
        skills_taught=skills,
        is_published=published,
    )
    session.add(program)
    await session.flush()
    return program


@pytest.mark.asyncio
async def test_topics_count_the_tags_of_published_courses_case_blind(client, session):
    await _tagged(session, skills=["Python", "sql"])
    await _tagged(
        session, skills=["python", "маркетинг"], category=ProgramCategory.ENTREPRENEURSHIP
    )
    await _tagged(session, skills=["python"], published=False)
    response = await client.get("/api/v1/programs/topics")
    assert response.status_code == 200
    by_label = {row["label"]: row for row in response.json()}
    assert by_label["python"]["count"] == 2  # the draft is not counted; case is folded
    assert by_label["sql"]["count"] == 1
    # "python" is filed under the broader "programming" skill but keeps its own name.
    assert by_label["python"]["title_i18n"]["en"] == "Python"
    # Within a category, only that category's subjects.
    narrowed = await client.get("/api/v1/programs/topics?category=entrepreneurship")
    labels = {row["label"] for row in narrowed.json()}
    assert "маркетинг" in labels and "sql" not in labels


@pytest.mark.asyncio
async def test_the_skill_filter_matches_a_whole_tag_case_blind(client, session):
    wanted = await _tagged(session, skills=["Python"])
    await _tagged(session, skills=["nosql"])
    response = await client.get("/api/v1/programs?skill=python")
    ids = {row["id"] for row in response.json()["items"]}
    assert str(wanted.id) in ids
    assert all(row["skills_taught"] != ["nosql"] for row in response.json()["items"])
    # A pattern character in the query is taken literally.
    assert (await client.get("/api/v1/programs?skill=%25")).json()["total"] == 0


@pytest.mark.asyncio
async def test_level_and_duration_filters_use_only_stated_figures(client, session):
    from app.core.constants import ProficiencyLevel

    short = await _tagged(session, skills=["a"], hours=8, level=ProficiencyLevel.BEGINNER)
    long = await _tagged(session, skills=["a"], hours=60, level=ProficiencyLevel.ADVANCED)
    unknown = await _tagged(session, skills=["a"], hours=None)
    ids = lambda r: {row["id"] for row in r.json()["items"]}  # noqa: E731
    assert str(short.id) in ids(await client.get("/api/v1/programs?duration=short&skill=a"))
    assert str(long.id) not in ids(await client.get("/api/v1/programs?duration=short&skill=a"))
    # A course that states no hours is left out of every duration filter.
    assert str(unknown.id) not in ids(await client.get("/api/v1/programs?duration=long&skill=a"))
    assert ids(await client.get("/api/v1/programs?level=advanced&skill=a")) == {str(long.id)}


@pytest.mark.asyncio
async def test_suggestions_offer_published_titles_that_contain_the_text(client, session):
    await _tagged(session, skills=["x"], title="Python asoslari")
    await _tagged(session, skills=["x"], title="Python chuqur", published=False)
    response = await client.get("/api/v1/programs/suggest?q=pyth")
    assert response.status_code == 200
    titles = [row["title_i18n"]["uz"] for row in response.json()]
    assert "Python asoslari" in titles and "Python chuqur" not in titles
    assert (await client.get("/api/v1/programs/suggest?q=p")).status_code == 422


@pytest.mark.asyncio
async def test_related_courses_share_a_subject_before_a_category(client, session):
    this = await _tagged(session, skills=["python", "django"])
    sibling = await _tagged(session, skills=["Django"], category=ProgramCategory.ENTREPRENEURSHIP)
    same_category = await _tagged(session, skills=["cooking"])
    draft = await _tagged(session, skills=["django"], published=False)
    response = await client.get(f"/api/v1/programs/{this.id}/related?limit=5")
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()]
    assert ids[0] == str(sibling.id)  # a shared subject outranks a shared category
    assert str(same_category.id) in ids
    assert str(this.id) not in ids and str(draft.id) not in ids


@pytest.mark.asyncio
async def test_spellings_of_one_skill_are_one_topic_and_one_filter(client, session):
    from app.seed_skills import load_skills

    await load_skills(session)
    uz = await _tagged(session, skills=["ingliz tili"])
    ru = await _tagged(session, skills=["английский язык"])
    await _tagged(session, skills=["html"])
    topics = {row["label"]: row for row in (await client.get("/api/v1/programs/topics")).json()}
    english = [row for row in topics.values() if row["slug"] == "english"]
    assert len(english) == 1 and english[0]["count"] == 2
    assert english[0]["title_i18n"]["ru"] == "Английский язык"
    # An acronym reads as one; an alias of a broader skill keeps its own name.
    assert topics["html"]["title_i18n"]["en"] == "HTML" and topics["html"]["slug"] == "html-css"
    found = {
        row["id"] for row in (await client.get("/api/v1/programs?skill=english")).json()["items"]
    }
    assert {str(uz.id), str(ru.id)} <= found
