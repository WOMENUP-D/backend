"""The publication gate: what a machine may put in front of a reader.

This is the boundary the whole ingest feature rests on. `models/news.py` says
a post never carries a verdict and always names its source; for anything a
human drafts that is an editorial rule enforced by review, and for anything the
AI drafts there is no review — so it is enforced here, in code, over facts
about the row.

The test that matters most is the first one below. The obvious way to write
this gate is `credibility_of(post) >= CREDIBILITY_TRUSTED`, and it is wrong:
`news_ranking.credibility_of` short-circuits on `post.credibility_score`, and
`news_ai.apply_analysis` writes that column from the model's own reply on every
model-backed run. A gate written that way asks the model whether the model
should be trusted, and any draft that answers `credibility: 95` publishes
itself. The column is therefore never read here, and the test builds a post
with it set to prove it.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.constants import NewsCategory
from app.models.news import NewsPost
from app.services.news_ai import Analysis
from app.services.news_ingest import publication_gate, source_is_trusted

CLEAN_BODY = (
    "Jahon sogʻliqni saqlash tashkiloti yangi hisobot eʼlon qildi.\n\n"
    "Hisobotda ayollar salomatligi boʻyicha maʼlumotlar keltirilgan.\n\n"
    "Batafsil maʼlumot uchun shifokorga murojaat qiling."
)


@pytest.fixture(autouse=True)
def auto_publish_on(monkeypatch):
    """The switch is on for these tests, so what is being asserted is the gate
    itself rather than the switch in front of it."""
    monkeypatch.setattr(settings, "news_ingest_auto_publish", True)


def _post(**kwargs) -> NewsPost:
    defaults = {
        "slug": "test-post-abcdef01",
        "category": NewsCategory.HEALTH,
        "title_i18n": {"uz": "JSST hisoboti", "ru": "Доклад ВОЗ", "en": "WHO report"},
        "summary_i18n": {"uz": "Qisqacha", "ru": "Кратко", "en": "Summary"},
        "body_i18n": {"uz": CLEAN_BODY, "ru": "Текст", "en": "Body"},
        "source_name": "World Health Organization",
        "source_url": "https://www.who.int/news/item/2026-01-01-report",
        "tags": ["salomatlik"],
    }
    return NewsPost(**{**defaults, **kwargs})


RULES = Analysis(topics=[], age_relevance={}, relevance=70, credibility=None, impact=60)
MODEL_SAYS_95 = Analysis(
    topics=[], age_relevance={}, relevance=90, credibility=95, impact=80, source="model"
)


# --- the defect this file exists for -------------------------------------


def test_a_model_cannot_certify_its_own_post_by_scoring_it_high():
    """The regression test for the gate's worst possible bug.

    `credibility_score = 99` is on the row, and the analysis says 95, and both
    of those numbers came from a model. The article is from a site nobody
    listed. It must not publish.
    """
    post = _post(
        source_name="Sogʻliq kanali",
        source_url="https://telegram.me/health-channel/1",
        credibility_score=99,
    )

    blocked = publication_gate(post, MODEL_SAYS_95)

    assert "source_not_trusted" in blocked
    assert "source_domain_not_allowed" in blocked


def test_a_model_cannot_certify_itself_by_naming_a_trusted_source_either():
    """The same hole one field over. `source_name` is written by the model, so
    a draft that calls a Telegram channel "World Health Organization" is making
    a claim, not providing evidence. Only the host — which ingestion copies
    from the search result and never from the draft — confers trust."""
    post = _post(
        source_name="World Health Organization",
        source_url="https://telegram.me/health-channel/1",
        credibility_score=99,
    )

    assert source_is_trusted(post) is False
    assert "source_not_trusted" in publication_gate(post, MODEL_SAYS_95)


def test_a_high_score_on_a_trusted_source_is_simply_not_consulted():
    """The other direction, so the rule reads as "the score is irrelevant"
    rather than "a high score is suspicious"."""
    trusted = _post(credibility_score=99)
    unscored = _post()

    assert publication_gate(trusted, MODEL_SAYS_95) == publication_gate(unscored, RULES) == []


# --- the rest of the truth table -----------------------------------------


def test_a_trusted_attributed_clean_post_may_publish():
    assert publication_gate(_post(), RULES) == []


def test_a_national_source_may_publish():
    """A portal for women in Uzbekistan that could only ever auto-publish the
    Lancet would be a strange portal."""
    post = _post(source_name="UzA", source_url="https://uza.uz/uz/posts/example")
    assert publication_gate(post, RULES) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("source_name", None), ("source_url", None), ("source_name", "   ")],
)
def test_an_unattributed_post_is_never_published(field: str, value):
    blocked = publication_gate(_post(**{field: value}), RULES)
    assert "unattributed" in blocked


def test_a_source_outside_the_allowlist_is_not_published():
    post = _post(source_name="Some Blog", source_url="https://example.com/a")
    blocked = publication_gate(post, RULES)
    assert "source_domain_not_allowed" in blocked
    assert "source_not_trusted" in blocked


def test_an_announcement_always_waits_for_a_person():
    """A deadline nobody verified is the worst thing this feed could carry, and
    the AI has no way to verify one."""
    blocked = publication_gate(_post(category=NewsCategory.ANNOUNCEMENT), RULES)
    assert blocked == ["category_needs_review"]


def test_a_post_that_reads_as_a_verdict_waits_for_a_person():
    post = _post(body_i18n={"uz": CLEAN_BODY, "ru": "Принимайте 500 мг в день.", "en": ""})
    assert "reads_as_a_verdict" in publication_gate(post, RULES)


def test_a_post_touching_violence_waits_for_a_person():
    """The same keyword filter the navigator runs before retrieval. A story
    about abuse is not a thing to auto-publish beside a diet article."""
    post = _post(title_i18n={"uz": "Oiladagi zoravonlik haqida", "ru": "", "en": ""})
    assert "safety_topic" in publication_gate(post, RULES)


def test_a_post_with_no_uzbek_is_not_published():
    """Uzbek is the primary locale. A post the portal cannot show in it is not
    finished, whatever else it got right."""
    post = _post(body_i18n={"uz": "", "ru": "Текст", "en": "Body"})
    assert "missing_primary_locale" in publication_gate(post, RULES)


def test_the_analysing_model_can_block_its_own_post_but_not_clear_one():
    """One-way. A low self-scored credibility adds a reason to wait; a high one
    removes nothing, which is what the first test in this file asserts."""
    doubtful = Analysis(
        topics=[], age_relevance={}, relevance=50, credibility=30, impact=50, source="model"
    )
    assert "editor_scored_credibility_low" in publication_gate(_post(), doubtful)


def test_the_switch_stops_everything_regardless_of_the_rest(monkeypatch):
    """`NEWS_INGEST_AUTO_PUBLISH=false` turns the whole feature into a drafting
    aid without changing a single other rule."""
    monkeypatch.setattr(settings, "news_ingest_auto_publish", False)
    assert publication_gate(_post(), RULES) == ["auto_publish_disabled"]
