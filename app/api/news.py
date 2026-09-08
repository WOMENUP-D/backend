"""News and announcements — the feed the portal opens on.

Public, like the programme catalogue: a visitor is shown the same feed she will
keep reading once she registers, so the first screen after sign-up is already
familiar rather than empty.

Two things are decided here rather than in the client. Adult health posts are
filtered out of the response for a minor and for a reader whose age we do not
know, so an age-inappropriate card never reaches the browser to be hidden by
CSS. And drafts are 404 for everyone but the people who edit them.

`for-you` sits beside the feed and is a different kind of thing: a ranking, not
a gate. It orders the same posts `list_news` returns by how much they are for
this particular reader — her age bracket and the subjects she chose — and
withholds none of them. Everything it demotes stays one tap away in the feed,
in its category, and in search. That separation is deliberate and load-bearing:
what a woman *may* read is decided by `age_gate` and by nothing else.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ContentDep, CurrentUserDep, DbSession, OptionalUserDep, client_ip
from app.core.constants import DataClassification, NewsCategory, NewsTopic, Role
from app.models.news import NewsPost
from app.models.profile import Profile
from app.schemas.auth import CurrentUser
from app.schemas.common import Page, PaginationParams
from app.schemas.news import (
    ForYouFeed,
    NewsCreate,
    NewsDetail,
    NewsIngestReport,
    NewsPreferences,
    NewsPreferencesUpdate,
    NewsRead,
    NewsUpdate,
    PersonalisedNewsRead,
)
from app.services.age_gate import age_from_profile, band_for_profile, is_minor
from app.services.audit_service import record_audit
from app.services.news_age import AGE_GROUPS, group_for_age, group_for_profile
from app.services.news_ai import analyse, apply_analysis
from app.services.news_ingest import AUDIT_ACTION, ingest_news
from app.services.news_ranking import rank

router = APIRouter(prefix="/news", tags=["news"])


# How many recent posts the personalised section considers. The ranking runs
# in Python — the score depends on the reader, so it cannot be an index — and
# this bounds that work. Wide enough that a genuinely relevant article from
# three weeks ago can still surface above today's filler, narrow enough that
# the query stays one page of rows.
FOR_YOU_WINDOW = 120


async def _reader_profile(session: AsyncSession, user: CurrentUser | None) -> Profile | None:
    if user is None:
        return None
    return await session.scalar(select(Profile).where(Profile.user_id == uuid.UUID(user.id)))


async def _reader_may_see_adult(session: AsyncSession, user: CurrentUser | None) -> bool:
    """Whether adult health content belongs in this reader's feed.

    A visitor has no profile and therefore no known age, which `age_gate` reads
    as "may be a minor" — the protective default the whole portal uses.
    """
    if user is None:
        return False
    return not is_minor(band_for_profile(await _reader_profile(session, user)))


def _chosen_interests(profile: Profile | None) -> list[NewsTopic]:
    """Her subscribed subjects, ignoring any value the vocabulary has dropped."""
    topics: list[NewsTopic] = []
    for value in (profile.news_interests if profile else None) or []:
        try:
            topics.append(NewsTopic(value))
        except ValueError:
            continue
    return topics


@router.get("", response_model=Page[NewsRead])
async def list_news(
    session: DbSession,
    user: OptionalUserDep,
    pagination: PaginationParams = Depends(),
    category: NewsCategory | None = None,
    tag: str | None = Query(default=None, max_length=60),
    search: str | None = Query(default=None, max_length=100),
) -> Page[NewsRead]:
    """The feed. Pinned posts first, then newest.

    Pinning is what an announcement needs and a chronological feed cannot give
    it: a deadline that closes on Friday has to stay above three articles
    published on Thursday.
    """
    stmt = select(NewsPost).where(NewsPost.is_published.is_(True))

    if not await _reader_may_see_adult(session, user):
        stmt = stmt.where(NewsPost.is_adult_only.is_(False))

    if category:
        stmt = stmt.where(NewsPost.category == category)
    if tag:
        stmt = stmt.where(NewsPost.tags.any(tag))
    if search:
        # Title and summary in every locale: someone typing "skrining" or
        # "рак груди" is describing a subject, not quoting a headline.
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                *(
                    func.lower(field.op("->>")(lang)).like(pattern)
                    for field in (NewsPost.title_i18n, NewsPost.summary_i18n)
                    for lang in ("uz", "ru", "en")
                ),
                func.lower(func.array_to_string(NewsPost.tags, " ")).like(pattern),
            )
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(
            NewsPost.is_pinned.desc(),
            NewsPost.published_at.desc().nullslast(),
            NewsPost.created_at.desc(),
        )
        .offset(pagination.offset)
        .limit(pagination.size)
    )
    return Page[NewsRead](
        items=[NewsRead.model_validate(post) for post in rows.scalars()],
        total=total,
        page=pagination.page,
        size=pagination.size,
    )


@router.get("/categories", response_model=dict[str, int])
async def news_counts(session: DbSession, user: OptionalUserDep) -> dict[str, int]:
    """How many published posts sit in each section, for the filter row.

    Counted with the same age filter as the feed, so a chip never promises
    eleven posts and then opens on nine.
    """
    stmt = select(NewsPost.category, func.count()).where(NewsPost.is_published.is_(True))
    if not await _reader_may_see_adult(session, user):
        stmt = stmt.where(NewsPost.is_adult_only.is_(False))
    rows = await session.execute(stmt.group_by(NewsPost.category))
    return {str(category): count for category, count in rows.all()}


@router.get("/for-you", response_model=ForYouFeed)
async def for_you(
    session: DbSession,
    user: OptionalUserDep,
    size: int = Query(default=6, ge=1, le=20),
) -> ForYouFeed:
    """The personalised section: the same posts, in her order.

    Ranking only. Every post here is in the main feed too, and every post this
    ranks last is still in the main feed, in its category and in search — a
    fifteen-year-old is not shown menopause research at the top of her screen,
    and is not prevented from finding it. Section 08 of the brief asks for
    exactly that distinction, and it is the reason this is a separate endpoint
    rather than an ordering flag on `list_news`.

    The adult-content gate still applies, unchanged: it runs on the query
    below before anything is ranked, because what she may read is not a
    ranking question.
    """
    profile = await _reader_profile(session, user)
    group = group_for_profile(profile)
    interests = _chosen_interests(profile)

    stmt = select(NewsPost).where(NewsPost.is_published.is_(True))
    if user is None or is_minor(band_for_profile(profile)):
        stmt = stmt.where(NewsPost.is_adult_only.is_(False))

    rows = await session.execute(
        stmt.order_by(
            NewsPost.published_at.desc().nullslast(),
            NewsPost.created_at.desc(),
        ).limit(FOR_YOU_WINDOW)
    )
    ranked = rank(list(rows.scalars()), group=group, interests=interests)

    items = []
    for post, breakdown in ranked[:size]:
        card = PersonalisedNewsRead.model_validate(post)
        card.reasons = list(breakdown.reasons)
        items.append(card)

    return ForYouFeed(
        items=items,
        # Neither her age nor a single chosen subject means there is nothing to
        # personalise on, and the client should not label the result as if
        # there were.
        personalised=group is not None or bool(interests),
        age_group=group,
        interests=interests,
    )


def _preferences(profile: Profile | None) -> NewsPreferences:
    """Her current settings, with the age traced back to where it came from."""
    age = age_from_profile(profile)
    source = None
    if profile is not None and profile.birth_date is not None:
        source = "birth_date"
    elif age is not None:
        source = "age_group"

    return NewsPreferences(
        age=age,
        age_group=group_for_age(age),
        age_source=source,
        interests=_chosen_interests(profile),
        available_interests=list(NewsTopic),
        available_age_groups=list(AGE_GROUPS),
    )


@router.get("/preferences", response_model=NewsPreferences)
async def read_preferences(session: DbSession, user: CurrentUserDep) -> NewsPreferences:
    """What the news-preferences screen renders.

    Deliberately free of scores. Section 10 of the brief is explicit: she sets
    an age and ticks subjects; the weighting behind them is our problem, not
    something to put in front of her.
    """
    return _preferences(await _reader_profile(session, user))


@router.put("/preferences", response_model=NewsPreferences)
async def update_preferences(
    payload: NewsPreferencesUpdate,
    session: DbSession,
    user: CurrentUserDep,
) -> NewsPreferences:
    """Set the age bracket and the subscribed subjects.

    An age typed here is stored as the bracket it falls in, not as a
    fabricated date of birth. Where she has given a real birth date that keeps
    winning — `age_gate.age_from_profile` prefers it — and the response says as
    much through `age_source`, so the screen can show the derived age as a fact
    instead of offering to overwrite it with a worse one.
    """
    profile = await _reader_profile(session, user)
    if profile is None:
        profile = Profile(user_id=uuid.UUID(user.id))
        session.add(profile)

    changes = payload.model_dump(exclude_unset=True)
    if "age" in changes:
        group = group_for_age(changes["age"])
        profile.age_group = group.value if group else None
    if changes.get("interests") is not None:
        # Deduplicated with the order she chose preserved: the screen renders
        # this list back to her, and a set would shuffle it between visits.
        seen: set[str] = set()
        chosen: list[str] = []
        for topic in payload.interests or []:
            if topic.value not in seen:
                seen.add(topic.value)
                chosen.append(topic.value)
        profile.news_interests = chosen

    await session.flush()
    return _preferences(profile)


@router.get("/{slug}", response_model=NewsDetail)
async def read_news(slug: str, session: DbSession, user: OptionalUserDep) -> NewsPost:
    post = await session.scalar(select(NewsPost).where(NewsPost.slug == slug))
    may_see_drafts = user is not None and user.has_role(Role.ADMIN, Role.TRAINER, Role.MODERATOR)

    if post is None or (not post.is_published and not may_see_drafts):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    # The gate has to hold on the direct link too. Filtering only the list
    # would leave the article one shared URL away from a twelve-year-old.
    if post.is_adult_only and not may_see_drafts and not await _reader_may_see_adult(session, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    return post


@router.post("", response_model=NewsDetail, status_code=status.HTTP_201_CREATED)
async def create_news(payload: NewsCreate, user: ContentDep, session: DbSession) -> NewsPost:
    """Draft a post. Editors and admins only; it is not published on creation."""
    exists = await session.scalar(select(NewsPost.id).where(NewsPost.slug == payload.slug))
    if exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Post with slug '{payload.slug}' already exists",
        )
    post = NewsPost(**payload.model_dump(), author_id=uuid.UUID(user.id))
    session.add(post)
    await session.flush()
    return post


@router.patch("/{post_id}", response_model=NewsDetail)
async def update_news(
    post_id: uuid.UUID, payload: NewsUpdate, user: ContentDep, session: DbSession
) -> NewsPost:
    post = await session.get(NewsPost, post_id)
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    changes = payload.model_dump(exclude_unset=True)
    if changes.get("is_published") and post.published_at is None:
        post.published_at = datetime.now(UTC)
    for field, value in changes.items():
        setattr(post, field, value)
    await session.flush()
    return post


@router.post("/{post_id}/analyse", response_model=NewsDetail)
async def analyse_news(
    post_id: uuid.UUID,
    user: ContentDep,
    session: DbSession,
    request: Request,
) -> NewsPost:
    """Have the AI news editor read a post and score it.

    Explicit rather than automatic on publish. Scoring is a judgement about who
    an article is for, an editor stays answerable for it, and a model call that
    fires as a side effect of pressing "publish" is one nobody reviews. With no
    API key — or a model that is refusing or unreachable — this still succeeds
    and stores the keyword-based reading, which is the same one the feed would
    have used anyway.
    """
    post = await session.get(NewsPost, post_id)
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    analysis = await analyse(post)
    apply_analysis(post, analysis)

    await record_audit(
        session,
        action="news.analyse",
        entity_type="news_post",
        entity_id=str(post.id),
        actor_id=uuid.UUID(user.id),
        actor_role=user.role.value,
        classification=DataClassification.INTERNAL,
        changes={
            "source": analysis.source,
            "topics": [topic.value for topic in analysis.topics],
            "age_relevance": analysis.age_relevance,
        },
        ip_address=client_ip(request),
    )
    await session.flush()
    return post


@router.post("/ingest", response_model=NewsIngestReport)
async def trigger_ingest(
    user: ContentDep,
    session: DbSession,
    request: Request,
    limit: int = Query(default=3, ge=1, le=10),
) -> NewsIngestReport:
    """Run the news search now, on this editor's authority.

    Bounded deliberately: each item costs a search, a page fetch and two model
    calls, so the request-path version is a spot check rather than a backfill.
    Use `python -m app.ingest_news` for a full run.

    Publishing is not this endpoint's decision either —
    `news_ingest.publication_gate` decides, exactly as it does on the timer.
    What an editor gets by pressing this is a run *now*, not a run with fewer
    rules. It does bypass `NEWS_INGEST_ENABLED`, because that switch controls
    the timer and a named editor pressing a button is not a timer; the audit
    row below names her.

    Placement note: `POST /ingest` cannot be shadowed by `GET /{slug}` — there
    is no `POST /{param}` route on this router at all — so this sits with the
    other editor actions rather than above the reader ones.
    """
    result = await ingest_news(session, limit=limit, force=True)
    await record_audit(
        session,
        action=AUDIT_ACTION,
        entity_type="news_post",
        actor_id=uuid.UUID(user.id),
        actor_role=user.role.value,
        classification=DataClassification.INTERNAL,
        changes={**asdict(result), "manual": True},
        ip_address=client_ip(request),
    )
    await session.flush()
    return NewsIngestReport(**asdict(result))
