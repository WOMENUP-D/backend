"""The parts of the news ingest that decide things without a model.

Everything here is offline on purpose. The ingest job runs unattended, three
times a day, and publishes to the portal's front page — so the checks that
stand between a search result and a reader have to be the kind that can be
argued with in a test rather than the kind that depend on what a model said
that afternoon.

Four of them matter enough to pin literally: a URL normalises to one key so the
same article is never posted twice, a domain match cannot be fooled by a
lookalike host, a slug is always a legal slug even for a title written entirely
in Cyrillic, and the adult-content flag can be raised by the text but never
lowered by the model.
"""

from __future__ import annotations

import re

import pytest

from app.core.config import settings
from app.core.constants import NewsCategory
from app.models.news import NewsPost
from app.services.news_ingest import (
    SLUG_STEM_MAX,
    VERDICT_MARKERS,
    build_slug,
    canonical_url,
    domain_allowed,
    force_adult_only,
    is_near_duplicate,
    url_hash,
)


def _post(**kwargs) -> NewsPost:
    """A post object with only what the offline checks read."""
    defaults = {
        "slug": "test-post",
        "category": NewsCategory.HEALTH,
        "title_i18n": {"uz": "Sarlavha", "ru": "Заголовок", "en": "Title"},
        "summary_i18n": {"uz": "Qisqacha", "ru": "Кратко", "en": "Summary"},
        "body_i18n": {"uz": "Matn", "ru": "Текст", "en": "Body"},
        "tags": [],
    }
    return NewsPost(**{**defaults, **kwargs})


# --- URL identity --------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("https://www.who.int/news/item", "http://who.int/news/item/"),
        ("https://who.int/news/item?utm_source=x", "https://who.int/news/item#top"),
        ("https://who.int/news/item/", "https://WHO.int/news/item"),
    ],
)
def test_the_same_article_reached_two_ways_is_one_key(first: str, second: str):
    """Campaign tags, a trailing slash, the scheme and the case of the host are
    all noise. Treating them as identity is how a job that runs three times a
    day re-posts the same WHO article forever."""
    assert canonical_url(first) == canonical_url(second)
    assert url_hash(first) == url_hash(second)


def test_two_different_articles_are_two_keys():
    assert url_hash("https://who.int/news/a") != url_hash("https://who.int/news/b")


def test_a_url_with_no_host_is_not_usable():
    """The gate reads an empty canonical form as "not a source", which is the
    safe direction: a bare path can never be attributed to anybody."""
    assert canonical_url("not-a-url") == ""


# --- the domain allowlist ------------------------------------------------


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://who.int/news/item", True),
        ("https://apps.who.int/news/item", True),
        ("https://www.who.int/news/item", True),
        # The two that a naive `endswith` gets wrong, which is the whole
        # reason `_host_matches` exists.
        ("https://evil-who.int/news/item", False),
        ("https://who.int.example.com/news/item", False),
        ("https://telegram.me/some-channel", False),
    ],
)
def test_a_lookalike_host_is_not_the_host(url: str, allowed: bool):
    assert domain_allowed(url) is allowed


def test_the_allowlist_is_the_operators_and_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(settings, "news_ingest_allowed_domains", ["example.uz"])
    assert domain_allowed("https://news.example.uz/a") is True
    assert domain_allowed("https://who.int/a") is False


# --- slugs ---------------------------------------------------------------


DIGEST = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"


@pytest.mark.parametrize(
    "title",
    [
        "Ayollar sogʻligʻi: yangi tadqiqot",
        "Соғлиқни сақлаш вазирлиги янги дастур эълон қилди",
        "Всемирная организация здравоохранения — новый доклад",
        "!!! ??? ...",
        "A" * 400,
    ],
)
def test_a_slug_is_always_a_legal_slug(title: str):
    """`NewsBase.slug` is `^[a-z0-9-]+$` with a 120-character ceiling, and the
    column is 120 too. A Cyrillic-only headline must not reduce to nothing and
    leave a feed of posts whose URLs are eight hex characters."""
    slug = build_slug(title, DIGEST)

    assert re.fullmatch(r"[a-z0-9-]+", slug), slug
    assert len(slug) <= SLUG_STEM_MAX + 9 <= 120
    assert not slug.startswith("-") and not slug.endswith("-")
    assert slug.endswith(DIGEST[:8])


def test_a_cyrillic_title_still_produces_words():
    slug = build_slug("Соғлиқни сақлаш", DIGEST)
    assert slug.startswith("sogliqni-saqlash")


def test_the_same_article_always_gets_the_same_slug():
    """Slug collisions are impossible by construction rather than by retry: the
    hash suffix separates two articles with one headline, and keeps one article
    stable across runs."""
    assert build_slug("Bir xil sarlavha", DIGEST) == build_slug("Bir xil sarlavha", DIGEST)
    assert build_slug("Bir xil sarlavha", DIGEST) != build_slug("Bir xil sarlavha", "0" * 64)


# --- the adult-content flag ----------------------------------------------


def test_adult_subject_matter_raises_the_flag_from_the_body():
    post = _post(body_i18n={"uz": "Homiladorlik davrida ovqatlanish haqida.", "ru": "", "en": ""})
    assert force_adult_only(post) is True


def test_the_model_may_raise_the_flag_and_never_lower_it():
    """`is_adult_only = payload_flag or force_adult_only(post)`. This asserts
    the second half: a draft that says `adult_only: false` about an article on
    contraception does not get to say so."""
    post = _post(summary_i18n={"uz": "Kontratseptsiya haqida", "ru": "", "en": ""})
    payload_flag = False
    assert (payload_flag or force_adult_only(post)) is True


def test_an_ordinary_article_is_not_marked_adult():
    post = _post(
        title_i18n={"uz": "Matematika olimpiadasi", "ru": "Олимпиада", "en": "Olympiad"},
        body_i18n={"uz": "Maktab oʻquvchilari uchun musobaqa.", "ru": "", "en": ""},
    )
    assert force_adult_only(post) is False


# --- the verdict vocabulary ----------------------------------------------


def test_every_verdict_marker_can_actually_match_something():
    """A regression test for two markers that could never fire: one mixed
    Latin and Cyrillic inside a single word, the other was a regex written for
    a substring test. Both are gone; nothing may replace them."""
    for marker in VERDICT_MARKERS:
        assert marker == marker.lower(), marker
        assert ".." not in marker, marker
        scripts = {
            "cyrillic" if "Ѐ" <= char <= "ӿ" else "latin" for char in marker if char.isalpha()
        }
        assert len(scripts) == 1, f"{marker!r} mixes scripts and can never match"


@pytest.mark.parametrize(
    "text",
    [
        "Принимайте 500 мг в день после еды.",
        "Doctors prescribe this at 400 mg per day.",
        "Bu dori sizni davolaydi.",
    ],
)
def test_a_dose_instruction_is_recognised_as_a_verdict(text: str):
    from app.services.age_gate import fold

    assert any(marker in fold(text) for marker in VERDICT_MARKERS)


def test_two_uzbek_markers_are_ordinary_words_and_will_over_trigger():
    """Stated rather than hidden. "iching" (drink) and "qabul qiling" (take,
    accept) fire on copy that is not a verdict at all — an article about
    drinking enough water trips this. That is an accepted cost: the consequence
    is a post that waits for an editor, and the consequence of the opposite
    mistake is a dose instruction on a national portal."""
    from app.services.age_gate import fold

    innocent = "Kuniga kamida sakkiz stakan suv iching."
    assert any(marker in fold(innocent) for marker in VERDICT_MARKERS)


# --- near-duplicate titles -----------------------------------------------


def test_the_same_story_under_a_slightly_different_headline_is_a_duplicate():
    recent = [{"uz": "JSST ayollar salomatligi boʻyicha yangi hisobot chiqardi"}]
    assert is_near_duplicate(
        {"uz": "JSST ayollar salomatligi boʻyicha yangi hisobot chiqardi."}, recent
    )


def test_a_different_story_is_not_a_duplicate():
    recent = [{"uz": "JSST ayollar salomatligi boʻyicha yangi hisobot chiqardi"}]
    assert not is_near_duplicate({"uz": "Toshkentda matematika olimpiadasi boshlandi"}, recent)


def test_a_story_titled_in_another_locale_is_still_caught():
    """The same story arrives titled in whichever language the source
    publishes in, so every locale of every recent post is compared."""
    recent = [{"ru": "ВОЗ выпустила новый доклад о здоровье женщин", "uz": ""}]
    assert is_near_duplicate({"uz": "ВОЗ выпустила новый доклад о здоровье женщин"}, recent)
