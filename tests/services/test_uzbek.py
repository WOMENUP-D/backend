"""Uzbek is shown in the reformed alphabet, whatever alphabet it arrived in."""

import pytest

from app.services.uzbek import has_old_digraphs, to_new_alphabet, to_old_alphabet


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Oʻz ishingizni boshlang", "Öz işingizni boşlang"),
        ("Toshkent shahri", "Toşkent şahri"),
        ("Chiroyli gʻoya", "Çiroyli ğoya"),
        ("Sogʻliqni saqlash", "Soğliqni saqlaş"),
        ("SHAHAR MARKAZI", "ŞAHAR MARKAZI"),
        ("Oʻzbekiston Respublikasi", "Özbekiston Respublikasi"),
        ("maʼlumot va taʼlim", "maʼlumot va taʼlim"),  # the glottal stop stays
        ("O‘rganish", "Örganiş"),  # a typographic quote standing in for ʻ
        ("to'g'ridan-to'g'ri", "töğridan-töğri"),  # the plain apostrophe, as typed
        ("To’g’ri yo’l", "Töğri yöl"),  # the right quote, as word processors make it
        ("ta'lim va ma'no", "ta'lim va ma'no"),  # not after o or g: the glottal stop stays
        ("YaTT yoki MChJ", "YaTT yoki MÇJ"),  # an abbreviation the heuristic would protect
        ("Öz işingiz", "Öz işingiz"),  # already reformed: untouched
        ("", ""),
        (None, ""),
    ],
)
def test_old_digraphs_become_the_reformed_letters(old, new):
    assert to_new_alphabet(old) == new


@pytest.mark.parametrize(
    "foreign",
    [
        "Photoshop asoslari",  # ph: not an Uzbek word
        "ChatGPT bilan ishlash",  # a capital inside the word
        "WomanUPʼda",  # w
        "Charpentier",  # protected name
        "Excel va Word",
        "https://womanup.uz/dasturlar/shahar",  # an address
        "info@womanup.uz",
    ],
)
def test_foreign_words_and_addresses_are_left_alone(foreign):
    converted = to_new_alphabet(foreign)
    for word in (
        "Photoshop",
        "ChatGPT",
        "WomanUP",
        "Charpentier",
        "Excel",
        "Word",
        "womanup.uz/dasturlar/shahar",
        "info@womanup.uz",
    ):
        if word in foreign:
            assert word in converted
    # The Uzbek words around them still change.
    assert to_new_alphabet("ChatGPT bilan ishlash") == "ChatGPT bilan işlaş"


def test_russian_and_english_sentences_are_not_what_this_is_for():
    """Callers pass Uzbek text; Russian has none of the digraphs anyway."""
    assert to_new_alphabet("Саломатлик ва муносабатлар") == "Саломатлик ва муносабатлар"


@pytest.mark.parametrize(
    ("new", "old"),
    [
        ("Toşkent şahri", "Toshkent shahri"),
        ("Özbekiston", "Oʻzbekiston"),
        ("ŞAHAR", "SHAHAR"),
        ("Çiroyli ğoya", "Chiroyli gʻoya"),
        ("ChatGPT", "ChatGPT"),
    ],
)
def test_the_inverse_for_search_terms(new, old):
    assert to_old_alphabet(new) == old


def test_detection():
    assert has_old_digraphs("Toshkent")
    assert not has_old_digraphs("Toşkent")
    assert not has_old_digraphs("Photoshop")
    assert not has_old_digraphs(None)


# --- English stays English (QA audit G-02) -----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "General English for First-Year Students",
        "Machine Learning and Data Mining",
        "Child Nutrition",
        "Women In Tech",
        "Business English",
        "Harvard: Leadership",
        "Choose",
        "Search",
        "ML School by REU Data Science Club",
        "Information Technology",
    ],
)
def test_english_titles_are_left_alone(text):
    assert to_new_alphabet(text) == text


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Shaxsiy maqsadlarni yozish", "Şaxsiy maqsadlarni yoziş"),
        ("Kichik biznes uchun moliyaviy savodxonlik", "Kiçik biznes uçun moliyaviy savodxonlik"),
        ("Digital Qashqadaryo", "Digital Qaşqadaryo"),
        ("Alfa Savdo MChJ", "Alfa Savdo MÇJ"),
    ],
)
def test_uzbek_still_converts_beside_english_words(old, new):
    assert to_new_alphabet(old) == new


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("tashkilotchi", "taşkilotçi"),
        ("ruscha", "rusça"),
        ("uchrashuv", "uçraşuv"),
        ("kuchni", "kuçni"),
    ],
)
def test_the_english_rules_spare_uzbek_words(old, new):
    """The shapes uzbek.ts rejected (tch, sch, chr, chn) stay out of the rules."""
    assert to_new_alphabet(old) == new
