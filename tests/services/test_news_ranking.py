"""The six-component score, and the line it must not cross.

The whole feature rests on one distinction: age *moves* an article, it never
removes one. Most of what follows exists to hold that line — a menopause study
ranks below adolescent health for a fifteen-year-old and stays in the list, and
an important article outranks a demographically perfect but trivial one.

No database here. `NewsPost` is constructed in memory: the ranker is pure, and
a test that needs PostgreSQL to check arithmetic is a test that will be skipped
on the machine where it matters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.constants import AgeGroup, MedicalTopic, NewsCategory, NewsTopic
from app.models.news import NewsPost
from app.services.news_ranking import (
    CREDIBILITY_TRUSTED,
    CREDIBILITY_UNSOURCED,
    NEUTRAL_INTEREST,
    WEIGHTS,
    credibility_of,
    freshness_of,
    matching_interests,
    rank,
    score_post,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def post(
    title: str,
    *,
    category: NewsCategory = NewsCategory.MEDICINE,
    days_ago: int = 1,
    source: str | None = "WHO",
    tags: list[str] | None = None,
    pinned: bool = False,
    **columns,
) -> NewsPost:
    # Defaulted rather than passed outright: a test that wants to set
    # `topics` or `age_relevance` must not collide with the empty ones every
    # other test relies on.
    columns.setdefault("age_relevance", {})
    columns.setdefault("topics", [])
    return NewsPost(
        slug=title.lower().replace(" ", "-")[:100],
        category=category,
        title_i18n={"ru": title, "en": title},
        summary_i18n={},
        body_i18n={},
        tags=tags or [],
        source_name=source,
        is_published=True,
        is_pinned=pinned,
        published_at=NOW - timedelta(days=days_ago),
        created_at=NOW - timedelta(days=days_ago),
        **columns,
    )


def order(posts: list[NewsPost], *, group, interests=None) -> list[str]:
    return [p.slug for p, _ in rank(posts, group=group, interests=interests, now=NOW)]


# --- the point of the feature -------------------------------------------


def test_a_teenager_gets_adolescent_health_above_menopause():
    teen_post = post("Менструальное здоровье подростков")
    older_post = post("Симптомы менопаузы")

    ranked = order([older_post, teen_post], group=AgeGroup.TEEN)
    assert ranked.index(teen_post.slug) < ranked.index(older_post.slug)


def test_a_fifty_year_old_gets_the_same_two_posts_the_other_way_round():
    """Same posts, same day, same source — only the reader changed."""
    teen_post = post("Менструальное здоровье подростков")
    older_post = post("Симптомы менопаузы")

    ranked = order([teen_post, older_post], group=AgeGroup.MATURE)
    assert ranked.index(older_post.slug) < ranked.index(teen_post.slug)


def test_demoting_is_not_removing():
    """Section 08, stated as code. The ranker returns a score for every post it
    is given and has no way to drop one — a fifteen-year-old who scrolls far
    enough still reaches the menopause article."""
    posts = [post("Симптомы менопаузы"), post("Менструальное здоровье подростков")]
    ranked = rank(posts, group=AgeGroup.TEEN, now=NOW)

    assert len(ranked) == 2
    assert all(breakdown.total > 0 for _, breakdown in ranked)


def test_age_relevance_cannot_by_itself_decide_the_order():
    """The weights are the policy: age carries the largest single share and
    still less than half, so importance and freshness can outvote it."""
    assert WEIGHTS["age_relevance"] == max(WEIGHTS.values())
    assert WEIGHTS["age_relevance"] < 0.5
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_an_important_article_outranks_a_perfectly_targeted_dull_one():
    """Section 05: a breast-cancer breakthrough belongs in a young woman's feed
    even though the baseline for her bracket is lower than for an article aimed
    squarely at her."""
    landmark = post(
        "Прорыв в лечении рака молочной железы",
        source="The Lancet",
        pinned=True,
        days_ago=0,
    )
    targeted = post(
        "Обычная заметка о менструальном здоровье",
        source=None,
        days_ago=40,
        relevance_score=40,
        impact_score=20,
    )

    ranked = order([targeted, landmark], group=AgeGroup.YOUNG)
    assert ranked[0] == landmark.slug


def test_an_unscored_post_ranks_on_the_rules_rather_than_a_default():
    """A post the AI editor has never seen is still age-aware: the derivation
    runs on its own text. This is the degrade-don't-fail path, and it is the
    default path for every post in the archive."""
    unscored = post("Симптомы менопаузы")
    assert unscored.age_relevance == {}

    teen = score_post(unscored, group=AgeGroup.TEEN, now=NOW)
    mature = score_post(unscored, group=AgeGroup.MATURE, now=NOW)
    assert mature.age_relevance > teen.age_relevance


def test_a_stored_map_overrides_the_keyword_reading():
    """An editor has to be able to say the rules read a post wrong."""
    overridden = post(
        "Симптомы менопаузы",
        age_relevance={"13-17": 95, "18-24": 95, "25-34": 95, "35-44": 95, "45-54": 95, "55+": 95},
    )
    assert score_post(overridden, group=AgeGroup.TEEN, now=NOW).age_relevance == 95


# --- interests -----------------------------------------------------------


def test_a_chosen_subject_is_matched_by_category():
    science = post("Открытие", category=NewsCategory.SCIENCE)
    assert matching_interests(science, [NewsTopic.SCIENCE]) == [NewsTopic.SCIENCE]


def test_a_chosen_subject_is_matched_by_medical_subtopic():
    article = post("Менопауза и сон", topics=[MedicalTopic.MENOPAUSE.value])
    assert NewsTopic.MENOPAUSE in matching_interests(article, [NewsTopic.MENOPAUSE])


def test_a_chosen_subject_is_matched_by_wording_when_it_has_no_column():
    """ "Technology" is not a section of this feed and not a medical subtopic;
    it exists only in the words of the article."""
    article = post("Женщины в искусственном интеллекте", category=NewsCategory.CAREER)
    assert matching_interests(article, [NewsTopic.TECHNOLOGY]) == [NewsTopic.TECHNOLOGY]


def test_choosing_nothing_is_not_a_statement_that_nothing_interests_her():
    """The preferences screen is optional, so silence scores neutral rather
    than penalising every post she has not opted into."""
    article = post("Открытие", category=NewsCategory.SCIENCE)
    assert score_post(article, group=AgeGroup.YOUNG, interests=[], now=NOW).interest == (
        NEUTRAL_INTEREST
    )


def test_interests_reorder_posts_of_equal_age_relevance():
    science = post("Открытие в генетике", category=NewsCategory.SCIENCE)
    career = post("Вакансии недели", category=NewsCategory.CAREER)

    with_science = order([career, science], group=None, interests=[NewsTopic.SCIENCE])
    with_career = order([science, career], group=None, interests=[NewsTopic.CAREER])
    assert with_science[0] == science.slug
    assert with_career[0] == career.slug


def test_the_seventeen_year_old_from_the_brief():
    """Section 07's worked example: age 17, interested in science, medicine and
    technology. STEM and adolescent health come first, menopause and
    osteoporosis last — and all of them are still in the list."""
    interests = [NewsTopic.SCIENCE, NewsTopic.MEDICINE, NewsTopic.TECHNOLOGY]
    posts = [
        post("Менопауза: новые рекомендации"),
        post("Остеопороз у пожилых женщин"),
        post("Женщина-учёный получила премию", category=NewsCategory.SCIENCE),
        post("Менструальное здоровье подростков"),
    ]

    ranked = order(posts, group=AgeGroup.TEEN, interests=interests)
    assert len(ranked) == 4
    assert set(ranked[:2]) == {
        "женщина-учёный-получила-премию",
        "менструальное-здоровье-подростков",
    }
    assert ranked[-1] in {"менопауза:-новые-рекомендации", "остеопороз-у-пожилых-женщин"}


# --- the other four components -------------------------------------------


def test_an_unsourced_health_claim_ranks_below_an_attributed_one():
    """A claim about medicine with nobody behind it is a rumour, and the feed
    should not need an editor to score that by hand."""
    assert credibility_of(post("X", source="WHO")) == CREDIBILITY_TRUSTED
    assert credibility_of(post("X", source=None)) == CREDIBILITY_UNSOURCED
    assert credibility_of(post("X", source="Some blog")) > CREDIBILITY_UNSOURCED


def test_an_explicit_credibility_score_wins_over_the_source_list():
    assert credibility_of(post("X", source=None, credibility_score=88)) == 88


def test_freshness_falls_with_age_and_never_leaves_the_range():
    yesterday = freshness_of(post("X", days_ago=0), now=NOW)
    last_week = freshness_of(post("X", days_ago=6), now=NOW)
    last_year = freshness_of(post("X", days_ago=300), now=NOW)

    assert yesterday > last_week > last_year
    assert 0 <= last_year <= 100


def test_a_naive_timestamp_does_not_break_the_comparison():
    """Rows read back from Postgres and rows built in a test do not always
    agree about tzinfo, and comparing the two raises rather than mis-sorts."""
    naive = post("X")
    naive.published_at = datetime(2026, 9, 2, 6, 0)
    assert freshness_of(naive, now=NOW) == 100

    aware = post("Y", days_ago=3)
    # Sorting the two together is where a tzinfo mismatch would actually blow
    # up, so the mixed list is the assertion.
    assert len(rank([naive, aware], group=AgeGroup.YOUNG, now=NOW)) == 2


def test_every_component_stays_inside_zero_to_one_hundred():
    article = post("X", relevance_score=999, impact_score=-4, credibility_score=100)
    breakdown = score_post(article, group=AgeGroup.SENIOR, now=NOW)
    assert all(0 <= value <= 100 for value in breakdown.components.values())
    assert 0 <= breakdown.total <= 100


def test_a_recommendation_says_why_it_is_there():
    """An unexplained recommendation is not acceptable output on this portal.
    The reasons are i18n keys, never sentences — the feed is read in three
    languages and the portal has no hardcoded UI strings."""
    article = post("Открытие", category=NewsCategory.SCIENCE, source="Nature")
    breakdown = score_post(article, group=AgeGroup.YOUNG, interests=[NewsTopic.SCIENCE], now=NOW)
    assert "interest:science" in breakdown.reasons
    assert all(" " not in reason for reason in breakdown.reasons)


def test_ties_break_towards_the_newer_post():
    older = post("Одинаковая новость", days_ago=2)
    older.slug = "older"
    newer = post("Одинаковая новость", days_ago=2)
    newer.slug = "newer"
    newer.published_at = NOW - timedelta(days=1, hours=23)

    assert order([older, newer], group=AgeGroup.YOUNG)[0] == "newer"
