"""The feed is public; the age boundary and the draft check are not negotiable.

The news feed is the first screen after registration and is readable without an
account, which makes two rules load-bearing rather than cosmetic.

* An adult-health post must not reach a minor, and must not reach a reader
  whose age we do not know — `age_gate` treats unknown as "may be a minor", and
  a visitor with no profile is exactly that. The gate has to hold on the direct
  link as well as in the listing: filtering only the list would leave the
  article one shared URL away from a twelve-year-old.
* A draft is not published content. Being signed in has never been authority to
  read one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import NewsCategory, Role
from app.core.security import create_token
from app.models.news import NewsPost
from app.models.profile import Profile
from app.models.user import User


async def _make_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    """A profile and an authored post both carry a foreign key to `users`, so
    the account behind a minted token has to actually exist."""
    user = User(id=user_id, phone=f"+9989{user_id.int % 10**8:08d}")
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def editor_headers(session: AsyncSession) -> dict[str, str]:
    """An editor whose row exists — `author_id` on a created post points at it."""
    editor_id = uuid.uuid4()
    await _make_user(session, editor_id)
    token = create_token(editor_id, Role.ADMIN, "access", {"roles": [Role.ADMIN.value]})
    return {"Authorization": f"Bearer {token}"}


async def _make_post(
    session: AsyncSession,
    *,
    published: bool = True,
    adult: bool = False,
    category: NewsCategory = NewsCategory.HEALTH,
    pinned: bool = False,
    slug: str | None = None,
) -> NewsPost:
    post = NewsPost(
        slug=slug or f"test-{uuid.uuid4().hex[:8]}",
        category=category,
        title_i18n={"uz": "Sarlavha", "ru": "Заголовок", "en": "Title"},
        summary_i18n={"uz": "Qisqacha"},
        body_i18n={"uz": "Matn"},
        source_name="WHO",
        tags=["test"],
        is_adult_only=adult,
        is_published=published,
        is_pinned=pinned,
        published_at=datetime.now(UTC) if published else None,
    )
    session.add(post)
    await session.flush()
    return post


async def _profile_aged(session: AsyncSession, user_id: uuid.UUID, years: int) -> None:
    today = date.today()
    await _make_user(session, user_id)
    session.add(
        Profile(
            user_id=user_id,
            birth_date=date(today.year - years, today.month, min(today.day, 28)),
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_feed_is_readable_without_an_account(client, session):
    await _make_post(session)
    response = await client.get("/api/v1/news")
    assert response.status_code == 200
    assert response.json()["total"] >= 1


@pytest.mark.asyncio
async def test_adult_post_is_withheld_from_a_visitor(client, session):
    """No account means no known age, and unknown is not adult."""
    adult = await _make_post(session, adult=True)
    body = (await client.get("/api/v1/news?size=100")).json()
    assert str(adult.id) not in {item["id"] for item in body["items"]}


@pytest.mark.asyncio
async def test_adult_post_is_404_on_the_direct_link_for_a_visitor(client, session):
    adult = await _make_post(session, adult=True)
    assert (await client.get(f"/api/v1/news/{adult.slug}")).status_code == 404


@pytest.mark.asyncio
async def test_adult_post_is_withheld_from_a_minor(client, session, auth_headers, user_id):
    await _profile_aged(session, user_id, 14)
    adult = await _make_post(session, adult=True)

    listing = (await client.get("/api/v1/news?size=100", headers=auth_headers)).json()
    assert str(adult.id) not in {item["id"] for item in listing["items"]}

    direct = await client.get(f"/api/v1/news/{adult.slug}", headers=auth_headers)
    assert direct.status_code == 404


@pytest.mark.asyncio
async def test_adult_post_reaches_an_adult(client, session, auth_headers, user_id):
    await _profile_aged(session, user_id, 30)
    adult = await _make_post(session, adult=True)

    listing = (await client.get("/api/v1/news?size=100", headers=auth_headers)).json()
    assert str(adult.id) in {item["id"] for item in listing["items"]}

    direct = await client.get(f"/api/v1/news/{adult.slug}", headers=auth_headers)
    assert direct.status_code == 200


@pytest.mark.asyncio
async def test_signed_in_without_a_profile_is_still_treated_as_a_minor(
    client, session, auth_headers
):
    """An account whose age we never learned is protected, not promoted."""
    adult = await _make_post(session, adult=True)
    listing = (await client.get("/api/v1/news?size=100", headers=auth_headers)).json()
    assert str(adult.id) not in {item["id"] for item in listing["items"]}


@pytest.mark.asyncio
async def test_category_counts_use_the_same_gate_as_the_feed(client, session):
    """A chip that promises a post the feed will not show is a broken chip."""
    await _make_post(session, adult=True, category=NewsCategory.MEDICINE)
    counts = (await client.get("/api/v1/news/categories")).json()
    assert counts.get("medicine", 0) == 0


@pytest.mark.asyncio
async def test_draft_is_hidden_from_the_feed_and_the_direct_link(client, session, auth_headers):
    draft = await _make_post(session, published=False)

    listing = (await client.get("/api/v1/news?size=100")).json()
    assert str(draft.id) not in {item["id"] for item in listing["items"]}

    assert (await client.get(f"/api/v1/news/{draft.slug}")).status_code == 404
    assert (await client.get(f"/api/v1/news/{draft.slug}", headers=auth_headers)).status_code == 404


@pytest.mark.asyncio
async def test_draft_is_visible_to_staff(client, session, admin_headers):
    draft = await _make_post(session, published=False)
    response = await client.get(f"/api/v1/news/{draft.slug}", headers=admin_headers)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_pinned_posts_sort_above_newer_ones(client, session):
    """A deadline closing on Friday has to stay above Thursday's articles."""
    await _make_post(session, slug="ordinary-newer")
    await _make_post(session, slug="pinned-older", pinned=True, category=NewsCategory.ANNOUNCEMENT)

    items = (await client.get("/api/v1/news?size=100")).json()["items"]
    assert items[0]["slug"] == "pinned-older"


@pytest.mark.asyncio
async def test_category_filter_narrows_the_feed(client, session):
    await _make_post(session, category=NewsCategory.SCIENCE, slug="science-one")
    await _make_post(session, category=NewsCategory.CAREER, slug="career-one")

    items = (await client.get("/api/v1/news?category=science&size=100")).json()["items"]
    assert {item["slug"] for item in items} == {"science-one"}


@pytest.mark.asyncio
async def test_creating_a_post_requires_an_editor(client, auth_headers):
    payload = {"slug": "nope", "category": "health", "title_i18n": {"uz": "X"}}
    assert (await client.post("/api/v1/news", json=payload)).status_code == 401
    assert (
        await client.post("/api/v1/news", json=payload, headers=auth_headers)
    ).status_code == 403


@pytest.mark.asyncio
async def test_a_new_post_is_not_published_by_the_act_of_creating_it(client, editor_headers):
    response = await client.post(
        "/api/v1/news",
        json={
            "slug": "editorial-draft",
            "category": "medicine",
            "title_i18n": {"uz": "Qoralama"},
            "cover_tone": "rose",
        },
        headers=editor_headers,
    )
    assert response.status_code == 201
    # It exists, but nobody can read it until an editor publishes it.
    assert (await client.get("/api/v1/news/editorial-draft")).status_code == 404


@pytest.mark.asyncio
async def test_unknown_cover_tone_is_rejected(client, editor_headers):
    """An unknown tone renders as an untinted grey card rather than fail loudly,
    so it is caught at the edge instead."""
    response = await client.post(
        "/api/v1/news",
        json={
            "slug": "bad-tone",
            "category": "health",
            "title_i18n": {"uz": "X"},
            "cover_tone": "neon",
        },
        headers=editor_headers,
    )
    assert response.status_code == 422
