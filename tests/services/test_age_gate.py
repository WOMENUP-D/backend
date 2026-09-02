"""The age band and the adult-health gate.

These are safety tests, not behaviour tests: the portal serves girls from 10,
and every case below is one where a wrong answer would put adult reproductive
content in front of a child.
"""

from datetime import date

import pytest

from app.core.constants import AgeBand
from app.models.profile import Profile
from app.services.age_gate import (
    age_from_profile,
    band_for_age,
    band_for_profile,
    blocks_adult_health,
    is_minor,
)

TODAY = date(2026, 8, 21)


def profile(**kwargs) -> Profile:
    return Profile(**kwargs)


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (10, AgeBand.CHILD),
        (12, AgeBand.CHILD),
        (13, AgeBand.TEEN),
        (17, AgeBand.TEEN),
        (18, AgeBand.ADULT),
        (45, AgeBand.ADULT),
        (None, AgeBand.UNKNOWN),
    ],
)
def test_band_boundaries(age, expected):
    assert band_for_age(age) is expected


def test_birthday_not_yet_reached_this_year_still_counts_as_younger():
    # Turns 18 in December; in August she is still 17 and must stay a minor.
    p = profile(birth_date=date(2008, 12, 31))
    assert age_from_profile(p, today=TODAY) == 17
    assert band_for_profile(p, today=TODAY) is AgeBand.TEEN


def test_age_group_is_used_when_there_is_no_birth_date():
    assert age_from_profile(profile(age_group="13-19")) == 13
    assert band_for_profile(profile(age_group="13-19")) is AgeBand.TEEN


def test_missing_age_is_not_treated_as_adult():
    """The whole point of UNKNOWN: absence of data must not unlock content."""
    band = band_for_profile(profile())
    assert band is AgeBand.UNKNOWN
    assert is_minor(band)


@pytest.mark.parametrize("band", [AgeBand.CHILD, AgeBand.TEEN, AgeBand.UNKNOWN])
@pytest.mark.parametrize(
    "question",
    [
        "Homiladorlik haqida gapirib bering",
        "Tug'ish qanday kechadi?",
        "Kontratsepsiya nima?",
        "Расскажите про беременность",
        "Что такое контрацепция?",
        "Tell me about pregnancy",
        "how does contraception work",
    ],
)
def test_adult_health_questions_are_blocked_for_minors(band, question):
    assert blocks_adult_health(question, band)


@pytest.mark.parametrize(
    "spelling",
    ["Tug'ish", "Tugʻish", "Tug‘ish", "Tugish"],
)
def test_apostrophe_spelling_does_not_defeat_the_gate(spelling):
    assert blocks_adult_health(f"{spelling} haqida savol", AgeBand.CHILD)


def test_adult_may_ask_adult_health_questions():
    assert not blocks_adult_health("Расскажите про беременность", AgeBand.ADULT)


@pytest.mark.parametrize(
    "question",
    [
        "Qanday qilib yaxshi uxlash mumkin?",
        "Как правильно питаться?",
        "How much exercise do I need?",
        "Menga sport haqida ayting",
    ],
)
def test_ordinary_health_questions_pass_for_every_band(question):
    for band in AgeBand:
        assert not blocks_adult_health(question, band)
