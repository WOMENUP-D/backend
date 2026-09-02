"""The personalised section, and the promise it makes to the rest of the feed.

`for-you` is the only surface on this portal that reorders content by who is
reading it, which makes one boundary worth testing from the outside rather than
trusting to the ranker: everything it pushes down must still be reachable. A
fifteen-year-old should not open the portal onto menopause research, and must
not be prevented from finding it — through the feed, a category or a search.

The other boundary is older and stricter: the adult-content gate. This section
does not get to widen it. A post a minor may not read must not appear here
either, whatever it scores.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import NewsCategory, Role
from app.core.security import create_token
from app.models.news import NewsPost
from app.models.profile import Profile
from app.models.user import User
from app.services.llm_gateway import LlmUnavailableError

TEEN_POST = "Менструальное здоровье подростков: новое исследование"
OLDER_POST = "Симптомы менопаузы: что известно"


async def _make_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = User(id=user_id, phone=f"+9989{user_id.int % 10**8:08d}")
    session.add(user)
    await session.flush()
    return user


async def _reader(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    years: int | None = None,
    interests: list[str] | None = None,
) -> Profile:
    await _make_user(session, user_id)
    today = date.today()
    profile = Profile(
        user_id=user_id,
        birth_date=(date(today.year - years, today.month, min(today.day, 28)) if years else None),
        news_interests=interests or [],
    )
    session.add(profile)
    await session.flush()
    return profile


async def _post(
    session: AsyncSession,
    title: str,
    *,
    slug: str,
    category: NewsCategory = NewsCategory.MEDICINE,
    adult: bool = False,
    days_ago: int = 1,
) -> NewsPost:
    post = NewsPost(
        slug=slug,
        category=category,
        title_i18n={"uz": title, "ru": title, "en": title},
        summary_i18n={"ru": title},
        body_i18n={"ru": title},
        source_name="WHO",
        tags=[],
        is_adult_only=adult,
        is_published=True,
        published_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    session.add(post)
    await session.flush()
    return post


@pytest_asyncio.fixture
async def two_posts(session: AsyncSession) -> None:
    """One article for teenagers, one for women past fifty. Same day, same
    source, same category — the age scores are the only thing separating
    them."""
    await _post(session, TEEN_POST, slug="teen-post")
    await _post(session, OLDER_POST, slug="older-post")


def _slugs(body: dict) -> list[str]:
    return [item["slug"] for item in body["items"]]


# --- the ordering ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_teenager_is_not_opened_onto_menopause_research(
    client, session, auth_headers, user_id, two_posts
):
    await _reader(session, user_id, years=15)
    body = (await client.get("/api/v1/news/for-you", headers=auth_headers)).json()

    slugs = _slugs(body)
    assert slugs.index("teen-post") < slugs.index("older-post")
    assert body["personalised"] is True
    assert body["age_group"] == "13-17"


@pytest.mark.asyncio
async def test_the_same_two_posts_invert_for_a_fifty_year_old(
    client, session, auth_headers, user_id, two_posts
):
    await _reader(session, user_id, years=50)
    slugs = _slugs((await client.get("/api/v1/news/for-you", headers=auth_headers)).json())
    assert slugs.index("older-post") < slugs.index("teen-post")


@pytest.mark.asyncio
async def test_what_is_demoted_is_still_in_the_feed_the_categories_and_the_search(
    client, session, auth_headers, user_id, two_posts
):
    """Section 08 of the brief, tested from the outside. Ranking last in "For
    you" must cost an article its position and nothing else."""
    await _reader(session, user_id, years=15)

    feed = _slugs((await client.get("/api/v1/news?size=100", headers=auth_headers)).json())
    assert "older-post" in feed

    category = await client.get("/api/v1/news?category=medicine&size=100", headers=auth_headers)
    assert "older-post" in _slugs(category.json())

    search = await client.get("/api/v1/news?search=менопаузы", headers=auth_headers)
    assert "older-post" in _slugs(search.json())

    direct = await client.get("/api/v1/news/older-post", headers=auth_headers)
    assert direct.status_code == 200


@pytest.mark.asyncio
async def test_the_main_feed_stays_chronological(client, session, auth_headers, user_id):
    """All News is not personalised. A reader comparing the two surfaces should
    find one ordered by date and the other by relevance — if the feed quietly
    reordered too, there would be no complete view of the news left."""
    await _reader(session, user_id, years=15)
    await _post(session, OLDER_POST, slug="newer-but-for-older", days_ago=0)
    await _post(session, TEEN_POST, slug="older-but-for-teens", days_ago=5)

    feed = _slugs((await client.get("/api/v1/news?size=100", headers=auth_headers)).json())
    assert feed.index("newer-but-for-older") < feed.index("older-but-for-teens")


# --- the gate above it ----------------------------------------------------


@pytest.mark.asyncio
async def test_an_adult_post_never_reaches_a_minor_through_this_section(
    client, session, auth_headers, user_id
):
    """Ranking does not get to reopen a door the age gate closed."""
    await _reader(session, user_id, years=15)
    await _post(session, "Контрацепция", slug="adult-post", adult=True)

    body = (await client.get("/api/v1/news/for-you", headers=auth_headers)).json()
    assert "adult-post" not in _slugs(body)


@pytest.mark.asyncio
async def test_an_adult_post_never_reaches_a_visitor_through_this_section(client, session):
    await _post(session, "Контрацепция", slug="adult-post", adult=True)
    body = (await client.get("/api/v1/news/for-you")).json()
    assert "adult-post" not in _slugs(body)


@pytest.mark.asyncio
async def test_a_draft_is_not_personalised_into_visibility(client, session):
    post = await _post(session, TEEN_POST, slug="draft-post")
    post.is_published = False
    await session.flush()

    assert "draft-post" not in _slugs((await client.get("/api/v1/news/for-you")).json())


# --- honesty about what we know ------------------------------------------


@pytest.mark.asyncio
async def test_a_visitor_gets_posts_but_is_not_told_they_are_personalised(
    client, session, two_posts
):
    """No account means no age and no chosen subjects. The ranking still runs
    on importance and freshness, and the client is told not to call it "For
    you" — a section named after a reader who supplied nothing is a lie with a
    headline on it."""
    body = (await client.get("/api/v1/news/for-you")).json()

    assert body["items"]
    assert body["personalised"] is False
    assert body["age_group"] is None


@pytest.mark.asyncio
async def test_an_account_with_no_age_and_no_interests_is_not_personalised(
    client, session, auth_headers, user_id, two_posts
):
    await _reader(session, user_id)
    body = (await client.get("/api/v1/news/for-you", headers=auth_headers)).json()
    assert body["personalised"] is False


@pytest.mark.asyncio
async def test_chosen_interests_alone_are_enough_to_personalise(
    client, session, auth_headers, user_id
):
    """Age is not the only signal. A woman who told us nothing but what she
    wants to read has told us something."""
    await _reader(session, user_id, interests=["science"])
    await _post(session, "Открытие", slug="science-post", category=NewsCategory.SCIENCE)
    await _post(session, "Вакансии", slug="career-post", category=NewsCategory.CAREER)

    body = (await client.get("/api/v1/news/for-you", headers=auth_headers)).json()
    assert body["personalised"] is True
    assert _slugs(body)[0] == "science-post"


@pytest.mark.asyncio
async def test_a_card_says_why_it_is_there(client, session, auth_headers, user_id):
    await _reader(session, user_id, years=30, interests=["science"])
    await _post(session, "Открытие", slug="science-post", category=NewsCategory.SCIENCE)

    body = (await client.get("/api/v1/news/for-you", headers=auth_headers)).json()
    card = next(item for item in body["items"] if item["slug"] == "science-post")
    assert "interest:science" in card["reasons"]


@pytest.mark.asyncio
async def test_the_reader_is_never_shown_the_maths(client, session, auth_headers, user_id):
    """Section 10: she sets an age and ticks subjects. The weighting behind
    them is our problem, not something to put in front of her."""
    await _reader(session, user_id, years=30)
    await _post(session, TEEN_POST, slug="any-post")

    card = (await client.get("/api/v1/news/for-you", headers=auth_headers)).json()["items"][0]
    assert not {"total", "score", "age_relevance", "relevance_score"} & set(card)


# --- preferences ----------------------------------------------------------


@pytest.mark.asyncio
async def test_preferences_need_an_account(client):
    assert (await client.get("/api/v1/news/preferences")).status_code == 401


@pytest.mark.asyncio
async def test_preferences_round_trip(client, session, auth_headers, user_id):
    await _make_user(session, user_id)

    saved = await client.put(
        "/api/v1/news/preferences",
        json={"age": 41, "interests": ["science", "medicine", "technology"]},
        headers=auth_headers,
    )
    assert saved.status_code == 200
    assert saved.json()["age_group"] == "35-44"

    read_back = (await client.get("/api/v1/news/preferences", headers=auth_headers)).json()
    assert read_back["interests"] == ["science", "medicine", "technology"]
    assert read_back["age_source"] == "age_group"
    assert "menopause" in read_back["available_interests"]
    assert read_back["available_age_groups"] == [
        "13-17",
        "18-24",
        "25-34",
        "35-44",
        "45-54",
        "55+",
    ]


@pytest.mark.asyncio
async def test_the_bracket_round_trips_exactly(client, session, auth_headers, user_id):
    """Only the bracket is stored, so `age` reads back as its lower bound and
    not as the number that was sent — 41 becomes 35. That is why the screen
    offers the six brackets instead of a number field: sending the bracket's
    own lower bound is the only input that survives the round trip unchanged,
    and a form that quietly rewrites her answer looks broken."""
    await _make_user(session, user_id)

    for bracket, lower in (("35-44", 35), ("13-17", 13), ("55+", 55)):
        saved = await client.put(
            "/api/v1/news/preferences", json={"age": lower}, headers=auth_headers
        )
        assert saved.json()["age_group"] == bracket
        assert saved.json()["age"] == lower


@pytest.mark.asyncio
async def test_a_real_birth_date_keeps_winning_and_says_so(client, session, auth_headers, user_id):
    """The brief asks for the date to be preferred where there is one. The
    screen has to be able to show that rather than offer to overwrite it."""
    await _reader(session, user_id, years=52)

    body = (await client.get("/api/v1/news/preferences", headers=auth_headers)).json()
    assert body["age"] == 52
    assert body["age_group"] == "45-54"
    assert body["age_source"] == "birth_date"


@pytest.mark.asyncio
async def test_saved_preferences_change_the_order(client, session, auth_headers, user_id):
    """End to end: the setting she chose is the setting that ranks her feed."""
    await _make_user(session, user_id)
    await _post(session, TEEN_POST, slug="teen-post")
    await _post(session, OLDER_POST, slug="older-post")

    await client.put("/api/v1/news/preferences", json={"age": 15}, headers=auth_headers)
    young = _slugs((await client.get("/api/v1/news/for-you", headers=auth_headers)).json())

    await client.put("/api/v1/news/preferences", json={"age": 58}, headers=auth_headers)
    old = _slugs((await client.get("/api/v1/news/for-you", headers=auth_headers)).json())

    assert young.index("teen-post") < young.index("older-post")
    assert old.index("older-post") < old.index("teen-post")


@pytest.mark.asyncio
async def test_a_subject_outside_the_vocabulary_is_rejected(client, session, auth_headers, user_id):
    await _make_user(session, user_id)
    response = await client.put(
        "/api/v1/news/preferences",
        json={"interests": ["astrology"]},
        headers=auth_headers,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_choices_are_collapsed_in_the_order_she_picked(
    client, session, auth_headers, user_id
):
    await _make_user(session, user_id)
    body = (
        await client.put(
            "/api/v1/news/preferences",
            json={"interests": ["medicine", "science", "medicine"]},
            headers=auth_headers,
        )
    ).json()
    assert body["interests"] == ["medicine", "science"]


# --- the editor's side ----------------------------------------------------


@pytest_asyncio.fixture
async def editor_headers(session: AsyncSession) -> dict[str, str]:
    editor_id = uuid.uuid4()
    await _make_user(session, editor_id)
    token = create_token(editor_id, Role.ADMIN, "access", {"roles": [Role.ADMIN.value]})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_analysing_a_post_requires_an_editor(client, session, auth_headers):
    post = await _post(session, TEEN_POST, slug="teen-post")
    assert (await client.post(f"/api/v1/news/{post.id}/analyse")).status_code == 401
    assert (
        await client.post(f"/api/v1/news/{post.id}/analyse", headers=auth_headers)
    ).status_code == 403


@pytest.mark.asyncio
async def test_analysis_succeeds_without_a_model_and_stores_the_rule_based_reading(
    client, session, editor_headers
):
    """Degrade, don't fail. With no API key configured — the state the whole
    suite runs in, see `disable_llm` — the endpoint still returns a complete,
    defensible reading: the same one the feed would have derived on the fly."""
    post = await _post(session, OLDER_POST, slug="older-post")

    body = (await client.post(f"/api/v1/news/{post.id}/analyse", headers=editor_headers)).json()

    assert body["topics"] == ["menopause"]
    assert set(body["age_relevance"]) == {"13-17", "18-24", "25-34", "35-44", "45-54", "55+"}
    assert body["age_relevance"]["45-54"] > body["age_relevance"]["13-17"]


@pytest.mark.asyncio
async def test_a_model_that_fails_mid_request_falls_back_rather_than_erroring(
    client, session, editor_headers, monkeypatch
):
    """The other half of the same rule. A configured key that then rate-limits,
    refuses or times out must not turn an editor's action into a 500."""

    class FailingGateway:
        enabled = True

        async def complete(self, **_kwargs):
            raise LlmUnavailableError("model rate limit reached")

    monkeypatch.setattr("app.services.news_ai.llm_gateway", FailingGateway())
    post = await _post(session, OLDER_POST, slug="older-post")

    response = await client.post(f"/api/v1/news/{post.id}/analyse", headers=editor_headers)
    assert response.status_code == 200
    assert response.json()["topics"] == ["menopause"]


@pytest.mark.asyncio
async def test_a_model_reply_is_clamped_before_it_is_stored(
    client, session, editor_headers, monkeypatch
):
    """The model is asked, the code enforces. A structured-output schema
    constrains shape, not values — so an out-of-range score or a subject
    outside the vocabulary is corrected here rather than written to the row."""

    class WildGateway:
        enabled = True

        async def complete(self, **_kwargs):
            return SimpleNamespace(
                refused=False,
                parsed={
                    "topics": ["menopause", "astrology"],
                    "age_relevance": {
                        "13-17": -40,
                        "18-24": 10,
                        "25-34": 30,
                        "35-44": 70,
                        "45-54": 900,
                        "55+": 90,
                    },
                    "relevance": 88,
                    "credibility": 95,
                    "impact": 70,
                    "audience": "women over forty",
                    "rationale": "menopause research",
                },
                trace_id="trace-1",
                model="claude-test",
                prompt_version="v1",
            )

    monkeypatch.setattr("app.services.news_ai.llm_gateway", WildGateway())
    post = await _post(session, OLDER_POST, slug="older-post")

    body = (await client.post(f"/api/v1/news/{post.id}/analyse", headers=editor_headers)).json()

    assert body["topics"] == ["menopause"]
    assert body["age_relevance"]["13-17"] == 0
    assert body["age_relevance"]["45-54"] == 100


@pytest.mark.asyncio
async def test_an_analysed_post_records_which_model_said_so(
    client, session, editor_headers, monkeypatch
):
    """Every AI output on this portal carries its trace: a ranking decision
    months from now has to be traceable to the reading that caused it."""

    class Gateway:
        enabled = True

        async def complete(self, **_kwargs):
            return SimpleNamespace(
                refused=False,
                parsed={
                    "topics": [],
                    "age_relevance": dict.fromkeys(
                        ["13-17", "18-24", "25-34", "35-44", "45-54", "55+"], 80
                    ),
                    "relevance": 80,
                    "credibility": 80,
                    "impact": 80,
                    "audience": "everyone",
                    "rationale": "general",
                },
                trace_id="trace-42",
                model="claude-test",
                prompt_version="v9",
            )

    monkeypatch.setattr("app.services.news_ai.llm_gateway", Gateway())
    post = await _post(session, TEEN_POST, slug="teen-post")

    await client.post(f"/api/v1/news/{post.id}/analyse", headers=editor_headers)
    await session.refresh(post)

    assert post.ai_meta["trace_id"] == "trace-42"
    assert post.ai_meta["model"] == "claude-test"
    assert post.ai_meta["prompt_version"] == "v9"
    assert post.ai_meta["source"] == "model"


@pytest.mark.asyncio
async def test_an_editor_can_overrule_the_keyword_reading(client, session, editor_headers):
    post = await _post(session, OLDER_POST, slug="older-post")
    flat = dict.fromkeys(["13-17", "18-24", "25-34", "35-44", "45-54", "55+"], 90)

    response = await client.patch(
        f"/api/v1/news/{post.id}",
        json={"age_relevance": flat, "topics": ["menopause"]},
        headers=editor_headers,
    )
    assert response.status_code == 200
    assert response.json()["age_relevance"] == flat


@pytest.mark.asyncio
async def test_a_typo_in_a_bracket_key_is_rejected_rather_than_ignored(
    client, session, editor_headers
):
    """A misspelt bracket would not fail, it would silently leave the post
    unscored for the reader it was written for."""
    post = await _post(session, OLDER_POST, slug="older-post")
    response = await client.patch(
        f"/api/v1/news/{post.id}",
        json={"age_relevance": {"45_54": 90}},
        headers=editor_headers,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_score_outside_the_range_is_rejected(client, session, editor_headers):
    post = await _post(session, OLDER_POST, slug="older-post")
    response = await client.patch(
        f"/api/v1/news/{post.id}",
        json={"age_relevance": {"45-54": 140}},
        headers=editor_headers,
    )
    assert response.status_code == 422
