"""News and announcements feed schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.constants import AgeGroup, MedicalTopic, NewsCategory, NewsTopic
from app.schemas.common import ORMModel

COVER_TONES = ("plum", "rose", "sand", "sage", "sky", "ink")


def _check_tone(value: str | None) -> str | None:
    """The tone picks a gradient from the site palette, so an unknown one would
    render as an untinted grey card rather than fail loudly."""
    if value is not None and value not in COVER_TONES:
        raise ValueError(f"cover_tone must be one of {', '.join(COVER_TONES)}")
    return value


def _check_age_relevance(value: dict | None) -> dict | None:
    """The age-relevance map: one 0-100 score per bracket, and no other keys.

    Checked at the edge rather than trusted, because this map arrives from two
    directions — an editor's form and the AI news editor — and a typo in a
    bracket key would not fail, it would silently rank the post as unscored
    for the reader it was meant for.
    """
    if value is None:
        return None
    allowed = {group.value for group in AgeGroup}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown age groups: {', '.join(unknown)}")
    for group, score in value.items():
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
            raise ValueError(f"age_relevance[{group}] must be a whole number from 0 to 100")
    return value


class NewsBase(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]+$", max_length=120)
    category: NewsCategory
    title_i18n: dict
    summary_i18n: dict = {}
    body_i18n: dict = {}
    cover_url: str | None = Field(default=None, max_length=500)
    cover_tone: str = "plum"
    cover_emblem: str = Field(default="✦", max_length=8)
    source_name: str | None = Field(default=None, max_length=160)
    source_url: str | None = Field(default=None, max_length=500)
    tags: list[str] = []
    region: str | None = None
    reading_minutes: int | None = Field(default=None, ge=1, le=90)
    is_adult_only: bool = False
    is_pinned: bool = False

    # --- editorial scoring; ranking only, never a filter -----------------
    # Left empty, the post is scored on the fly from its own text by
    # `services.news_age`. Set, these override that reading — which is the
    # point: keyword rules get some posts wrong and an editor must be able to
    # say so without changing the rules for everyone.
    age_relevance: dict[str, int] = {}
    topics: list[MedicalTopic] = []
    relevance_score: int | None = Field(default=None, ge=0, le=100)
    credibility_score: int | None = Field(default=None, ge=0, le=100)
    impact_score: int | None = Field(default=None, ge=0, le=100)

    _tone = field_validator("cover_tone")(_check_tone)
    _age = field_validator("age_relevance")(_check_age_relevance)


class NewsCreate(NewsBase):
    pass


class NewsUpdate(BaseModel):
    category: NewsCategory | None = None
    title_i18n: dict | None = None
    summary_i18n: dict | None = None
    body_i18n: dict | None = None
    cover_url: str | None = None
    cover_tone: str | None = None
    cover_emblem: str | None = Field(default=None, max_length=8)
    source_name: str | None = None
    source_url: str | None = None
    tags: list[str] | None = None
    reading_minutes: int | None = None
    is_adult_only: bool | None = None
    is_pinned: bool | None = None
    is_published: bool | None = None
    age_relevance: dict[str, int] | None = None
    topics: list[MedicalTopic] | None = None
    relevance_score: int | None = Field(default=None, ge=0, le=100)
    credibility_score: int | None = Field(default=None, ge=0, le=100)
    impact_score: int | None = Field(default=None, ge=0, le=100)

    _tone = field_validator("cover_tone")(_check_tone)
    _age = field_validator("age_relevance")(_check_age_relevance)


class NewsRead(ORMModel):
    """One card in the feed. Carries the summary, never the whole body — the
    feed is scrolled, and shipping every article to render a list of cards is
    what makes a feed slow on a regional connection."""

    id: uuid.UUID
    slug: str
    category: NewsCategory
    title_i18n: dict
    summary_i18n: dict
    cover_url: str | None
    cover_tone: str
    cover_emblem: str
    source_name: str | None
    source_url: str | None
    tags: list[str]
    region: str | None
    reading_minutes: int | None
    is_pinned: bool
    published_at: datetime | None


class NewsDetail(NewsRead):
    body_i18n: dict
    is_adult_only: bool
    # Present so an editor can see what the article was scored as before
    # overriding it. Not rendered to a reader: section 10 of the brief is
    # explicit that the personalisation maths is not a user-facing thing.
    topics: list[str]
    age_relevance: dict[str, int]


class PersonalisedNewsRead(NewsRead):
    """A card in the "For you" section.

    Carries `reasons` and nothing else extra: i18n keys such as
    `interest:science` or `age`, which the client renders as a short line
    saying why this post is here. An unexplained recommendation is not
    acceptable output on this portal, and a number would not be an
    explanation.
    """

    reasons: list[str] = []


class ForYouFeed(BaseModel):
    """The personalised section, and an honest statement of what it is.

    `personalised` is false when we know neither her age nor her interests. The
    ranking still runs — on importance and freshness — but the client is told
    not to call the result "For you", because a section named after a reader
    who supplied nothing is a lie with a headline on it.
    """

    items: list[PersonalisedNewsRead] = []
    personalised: bool = False
    age_group: AgeGroup | None = None
    interests: list[NewsTopic] = []


class NewsPreferences(BaseModel):
    """What the news-preferences screen shows her.

    `age` and `age_group` are derived, not stored twice: `age_source` says
    which profile field they came from so the screen can show a date of birth
    as a fact rather than offering to overwrite it.
    """

    age: int | None = None
    age_group: AgeGroup | None = None
    age_source: str | None = Field(
        default=None, description="birth_date | age_group — where the age was read from"
    )
    interests: list[NewsTopic] = []
    available_interests: list[NewsTopic] = []
    available_age_groups: list[AgeGroup] = []


class NewsPreferencesUpdate(BaseModel):
    """Only what she can actually change here.

    An age given here is stored as the bracket it falls in, not as a date of
    birth we would be inventing — so what comes back is the bracket, and a
    client that wants an exact number echoed back should not be asking for one
    it never persisted. The screen therefore offers the six brackets rather
    than a free number field.

    Where a real birth date already exists it keeps winning — see
    `age_gate.age_from_profile` — and the response says so through
    `age_source`.
    """

    age: int | None = Field(default=None, ge=10, le=120)
    interests: list[NewsTopic] | None = None
