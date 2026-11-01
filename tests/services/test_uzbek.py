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
