"""Ranking the personalised feed — the six-component score from section 06.

    Final score = relevance + credibility + impact + freshness
                + age relevance + user interest

Stated as a sum in the brief, implemented as a weighted average here, for one
reason: the brief also insists that age must *move* an article rather than
remove it. With six equal terms a zero on age relevance costs a post a sixth
of its score and a menopause study lands in a fifteen-year-old's "For you"
section anyway; with age weighted heaviest but still a minority of the total,
an important article about breast cancer outranks a mediocre one aimed exactly
at her bracket. That is the behaviour section 05 asks for.

The weights are the whole editorial policy, so they are one visible table
rather than magic numbers scattered through the function.

What this module does not do is hide anything. It produces a score. The feed
in `api.news.list_news` never calls it: All News stays chronological and
complete, categories and search stay whole, and the only surface that reads a
score is the "For you" section, which is by definition a selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.constants import AgeGroup, MedicalTopic, NewsCategory, NewsTopic
from app.models.news import NewsPost
from app.services.age_gate import fold
from app.services.news_age import (
    derive_age_relevance,
    relevance_for,
    searchable_text,
)

# What each component contributes. Age relevance carries the largest single
# share — it is the point of the feature — but stays a minority of the whole,
# which is what keeps it a ranking signal instead of a filter.
WEIGHTS: dict[str, float] = {
    "relevance": 0.15,
    "credibility": 0.10,
    "impact": 0.15,
    "freshness": 0.20,
    "age_relevance": 0.25,
    "interest": 0.15,
}

# Scores for a post nobody has assessed. Neutral rather than optimistic: an
# unscored post should neither outrank a scored one nor be buried under it.
DEFAULT_RELEVANCE = 70
DEFAULT_IMPACT = 60
DEFAULT_AGE_RELEVANCE = 70
NEUTRAL_INTEREST = 70

# Sources whose name alone is an argument. A claim about medicine on a state
# portal is only as good as who is making it, and this list is why the feed
# can rank an unattributed post below an attributed one without an editor
# having to score every single row by hand.
TRUSTED_SOURCES: tuple[str, ...] = (
    "who",
    "world health organization",
    "jahon sogliqni saqlash tashkiloti",
    "всемирная организация здравоохранения",
    "unicef",
    "unesco",
    "unfpa",
    "un women",
    "nobel",
    "lancet",
    "nature",
    "science",
    "bmj",
    "nejm",
    "new england journal",
    "cdc",
    "ema",
    "fda",
    "cochrane",
    "womanup",
    "vazirlik",  # a ministry of the Republic
    "министерств",
    "ministry",
)

CREDIBILITY_TRUSTED = 95
CREDIBILITY_ATTRIBUTED = 75
CREDIBILITY_UNSOURCED = 40

# A pinned post is an editor saying "this one matters this week", which is an
# impact judgement already made by a person.
IMPACT_PINNED = 90

# --- what a chosen interest actually matches -----------------------------
#
# An interest is a reader's word for a subject; a post carries a category, a
# set of medical subtopics and some tags. This is the join between them.

INTEREST_CATEGORIES: dict[NewsTopic, frozenset[NewsCategory]] = {
    NewsTopic.SCIENCE: frozenset({NewsCategory.SCIENCE}),
    NewsTopic.MEDICINE: frozenset({NewsCategory.MEDICINE}),
    NewsTopic.CAREER: frozenset({NewsCategory.CAREER}),
    NewsTopic.EDUCATION: frozenset({NewsCategory.EDUCATION}),
    NewsTopic.WOMEN_IN_STEM: frozenset({NewsCategory.SCIENCE}),
}

INTEREST_MEDICAL_TOPICS: dict[NewsTopic, frozenset[MedicalTopic]] = {
    NewsTopic.MEDICINE: frozenset(MedicalTopic),
    NewsTopic.MENTAL_HEALTH: frozenset({MedicalTopic.MENTAL_HEALTH}),
    NewsTopic.REPRODUCTIVE_HEALTH: frozenset(
        {
            MedicalTopic.MENSTRUAL_HEALTH,
            MedicalTopic.PMS,
            MedicalTopic.PCOS,
            MedicalTopic.FERTILITY,
            MedicalTopic.CONTRACEPTION,
            MedicalTopic.PREGNANCY,
            MedicalTopic.POSTPARTUM,
        }
    ),
    NewsTopic.PREVENTION: frozenset(
        {
            MedicalTopic.PREVENTION,
            MedicalTopic.BREAST_HEALTH,
            MedicalTopic.CERVICAL_HEALTH,
            MedicalTopic.CARDIOVASCULAR,
        }
    ),
    NewsTopic.NUTRITION: frozenset({MedicalTopic.NUTRITION}),
    NewsTopic.MENOPAUSE: frozenset(
        {
            MedicalTopic.PERIMENOPAUSE,
            MedicalTopic.MENOPAUSE,
            MedicalTopic.POSTMENOPAUSE,
            MedicalTopic.OSTEOPOROSIS,
        }
    ),
    NewsTopic.FAMILY: frozenset({MedicalTopic.PREGNANCY, MedicalTopic.POSTPARTUM}),
}

# Interests with no category and no subtopic of their own are matched on the
# text, folded the same way `news_age` folds its keywords.
INTEREST_KEYWORDS: dict[NewsTopic, tuple[str, ...]] = {
    NewsTopic.TECHNOLOGY: (
        "texnologiya",
        "raqamli",
        "suniy intellekt",
        "dasturlash",
        "it ",
        "технологи",
        "цифров",
        # Both words of "искусственный интеллект" inflect, so the phrase
        # itself never matches a real sentence. The second stem does.
        "интеллект",
        "нейросет",
        "программирован",
        "technology",
        "digital",
        "artificial intelligence",
        " ai ",
        "software",
    ),
    NewsTopic.WOMEN_IN_STEM: (
        "stem",
        "ilm-fan",
        "olima",
        "tadqiqotchi",
        "женщин в науке",
        "учёная",
        "учена",
        "исследовательниц",
        "women in science",
        "woman scientist",
        "researcher",
    ),
    NewsTopic.FAMILY: (
        "oila",
        "farzand",
        "onalik",
        "семь",
        "материнств",
        "воспитани детей",
        "family",
        "parenting",
        "motherhood",
    ),
    NewsTopic.CAREER: (
        "ish orni",
        "vakansiya",
        "tadbirkor",
        "карьер",
        "ваканси",
        "предпринимат",
        "career",
        "vacancy",
        "entrepreneur",
    ),
    NewsTopic.EDUCATION: (
        "talim",
        "kurs",
        "stipendiya",
        "образовани",
        "стипенди",
        "курс",
        "education",
        "scholarship",
        "course",
    ),
}


@dataclass(frozen=True)
class Breakdown:
    """One post's score, component by component.

    Kept whole rather than reduced to a number because an unexplainable
    ranking is not something this portal ships: an editor debugging why an
    article sits where it does needs the parts, and the AI guardrails require
    a recommendation to be explainable. The reader is shown none of it —
    section 10 of the brief is explicit that scores are not a user-facing
    thing — only `reasons`, which are i18n keys the client renders as words.
    """

    relevance: int
    credibility: int
    impact: int
    freshness: int
    age_relevance: int
    interest: int
    total: float
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def components(self) -> dict[str, int]:
        return {
            "relevance": self.relevance,
            "credibility": self.credibility,
            "impact": self.impact,
            "freshness": self.freshness,
            "age_relevance": self.age_relevance,
            "interest": self.interest,
        }


def credibility_of(post: NewsPost) -> int:
    """How much weight the source behind this post carries.

    Derived rather than required: an editor scoring every row by hand is an
    editor who will stop doing it, and an unsourced health post ranking below
    a WHO one should be automatic.
    """
    if post.credibility_score is not None:
        return max(0, min(100, post.credibility_score))
    if not post.source_name:
        return CREDIBILITY_UNSOURCED
    name = fold(post.source_name)
    if any(trusted in name for trusted in TRUSTED_SOURCES):
        return CREDIBILITY_TRUSTED
    return CREDIBILITY_ATTRIBUTED


def freshness_of(post: NewsPost, *, now: datetime | None = None) -> int:
    """How current the post is.

    Stepped rather than a smooth decay so that the order of a day's posts does
    not churn between two requests a minute apart, and so the thresholds can
    be read and argued with.
    """
    published = post.published_at or post.created_at
    if published is None:
        return 50
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    days = ((now or datetime.now(UTC)) - published).total_seconds() / 86400
    for limit, score in ((1, 100), (3, 88), (7, 75), (14, 60), (30, 45), (90, 30)):
        if days < limit:
            return score
    return 15


def age_relevance_of(post: NewsPost, group: AgeGroup | None) -> int:
    """This post's score for one bracket.

    Falls back through three levels, which is the degrade-don't-fail rule the
    rest of the AI layer follows: the stored map first, then the rule-based
    derivation from the post's own text, then a neutral default when we do not
    know the reader's age at all.
    """
    if group is None:
        return DEFAULT_AGE_RELEVANCE

    stored = post.age_relevance or None
    if not stored:
        stored = derive_age_relevance(
            category=post.category,
            title_i18n=post.title_i18n,
            summary_i18n=post.summary_i18n,
            tags=list(post.tags or []),
            topics=_topics_of(post) or None,
        )
    return relevance_for(stored, group, default=DEFAULT_AGE_RELEVANCE)


def _published_key(post: NewsPost) -> datetime:
    """A comparable publication time for tie-breaking.

    Normalised to UTC-aware because a row read back from Postgres and one
    built in a test are not guaranteed to agree about tzinfo, and comparing
    the two raises rather than mis-sorts.
    """
    stamp = post.published_at or post.created_at
    if stamp is None:
        return datetime.min.replace(tzinfo=UTC)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _topics_of(post: NewsPost) -> list[MedicalTopic]:
    """The post's stored subtopics, ignoring any value no longer in the enum."""
    topics = []
    for value in post.topics or []:
        try:
            topics.append(MedicalTopic(value))
        except ValueError:
            continue
    return topics


def matching_interests(post: NewsPost, interests: list[NewsTopic]) -> list[NewsTopic]:
    """Which of her chosen subjects this post is actually about."""
    if not interests:
        return []

    topics = set(_topics_of(post))
    text = fold(searchable_text(post.title_i18n, post.summary_i18n, list(post.tags or [])))

    matched: list[NewsTopic] = []
    for interest in interests:
        if post.category in INTEREST_CATEGORIES.get(interest, frozenset()):
            matched.append(interest)
            continue
        if topics & INTEREST_MEDICAL_TOPICS.get(interest, frozenset()):
            matched.append(interest)
            continue
        if any(word in text for word in INTEREST_KEYWORDS.get(interest, ())):
            matched.append(interest)
    return matched


def interest_score(matched: list[NewsTopic], chosen: list[NewsTopic]) -> int:
    """How well the post answers what she said she wanted to read.

    A reader who has chosen nothing gets the neutral value rather than a
    penalty: silence is not a statement that nothing interests her, and the
    preferences screen is optional.
    """
    if not chosen:
        return NEUTRAL_INTEREST
    if not matched:
        return 30
    return min(100, 75 + 12 * (len(matched) - 1))


def score_post(
    post: NewsPost,
    *,
    group: AgeGroup | None,
    interests: list[NewsTopic] | None = None,
    now: datetime | None = None,
) -> Breakdown:
    """The full six-component score for one post and one reader."""
    chosen = list(interests or [])
    matched = matching_interests(post, chosen)

    relevance = (
        max(0, min(100, post.relevance_score))
        if post.relevance_score is not None
        else DEFAULT_RELEVANCE
    )
    credibility = credibility_of(post)
    impact = (
        max(0, min(100, post.impact_score))
        if post.impact_score is not None
        else (IMPACT_PINNED if post.is_pinned else DEFAULT_IMPACT)
    )
    freshness = freshness_of(post, now=now)
    age = age_relevance_of(post, group)
    interest = interest_score(matched, chosen)

    parts = {
        "relevance": relevance,
        "credibility": credibility,
        "impact": impact,
        "freshness": freshness,
        "age_relevance": age,
        "interest": interest,
    }
    total = sum(parts[name] * weight for name, weight in WEIGHTS.items())

    # Reasons are i18n keys, not sentences: the portal has no hardcoded UI
    # strings and the feed is read in three languages.
    reasons: list[str] = [f"interest:{topic.value}" for topic in matched]
    if group is not None and age >= 80:
        reasons.append("age")
    if impact >= IMPACT_PINNED or credibility >= CREDIBILITY_TRUSTED:
        reasons.append("important")

    return Breakdown(
        relevance=relevance,
        credibility=credibility,
        impact=impact,
        freshness=freshness,
        age_relevance=age,
        interest=interest,
        total=round(total, 2),
        reasons=tuple(reasons),
    )


def rank(
    posts: list[NewsPost],
    *,
    group: AgeGroup | None,
    interests: list[NewsTopic] | None = None,
    now: datetime | None = None,
) -> list[tuple[NewsPost, Breakdown]]:
    """Posts newest-relevant first, each with the score that put it there.

    Ties break on recency: two posts a reader has equal reason to read should
    arrive in the order they were published.
    """
    scored = [(post, score_post(post, group=group, interests=interests, now=now)) for post in posts]
    scored.sort(key=lambda pair: (pair[1].total, _published_key(pair[0])), reverse=True)
    return scored
