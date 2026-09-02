"""Age relevance: the numbers behind "who is this article for".

The specification states its expectations as examples rather than as formulas —
menopause research near zero for a fifteen-year-old and near the top for a
fifty-year-old, adolescent menstrual health the other way round, a scientific
prize flat across the whole range. These tests hold the derivation to the
*shape* of those examples rather than to their exact digits: the brief itself
says the table is a baseline and the final call is editorial, so pinning
`45-54` to precisely 100 would be testing a number nobody promised.

What is not negotiable, and is asserted literally, is that this module ranks
and never blocks. Every score stays inside 0-100, every bracket always gets
one, and nothing here has any way to remove a post from anything.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.constants import AgeGroup, MedicalTopic, NewsCategory
from app.models.profile import Profile
from app.services.news_age import (
    AGE_GROUPS,
    IMPORTANCE_FLOOR,
    derive_age_relevance,
    detect_medical_topics,
    group_for_age,
    group_for_profile,
    relevance_for,
)


def scores(title: str, category: NewsCategory = NewsCategory.MEDICINE) -> dict[str, int]:
    return derive_age_relevance(category=category, title_i18n={"ru": title, "en": title})


# --- brackets ------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (13, AgeGroup.TEEN),
        (15, AgeGroup.TEEN),
        (17, AgeGroup.TEEN),
        (18, AgeGroup.YOUNG),
        (24, AgeGroup.YOUNG),
        (25, AgeGroup.EARLY_ADULT),
        (34, AgeGroup.EARLY_ADULT),
        (35, AgeGroup.MID_ADULT),
        (44, AgeGroup.MID_ADULT),
        (45, AgeGroup.MATURE),
        (54, AgeGroup.MATURE),
        (55, AgeGroup.SENIOR),
        (80, AgeGroup.SENIOR),
    ],
)
def test_every_boundary_of_the_six_brackets(age, expected):
    assert group_for_age(age) == expected


def test_a_girl_under_thirteen_is_ranked_as_a_teenager():
    """The portal is open from ten and the youngest bracket starts at thirteen.

    She is ranked with the teenagers rather than given a bracket of her own —
    and what she may actually *see* is still `age_gate`'s decision, which is a
    stricter one.
    """
    assert group_for_age(11) is AgeGroup.TEEN


def test_an_unknown_age_is_not_guessed():
    """None means none. Coercing it to a bracket would push articles at a
    reader on the strength of a number nobody supplied."""
    assert group_for_age(None) is None
    assert group_for_profile(None) is None
    assert group_for_profile(Profile()) is None


def test_a_birth_date_beats_a_typed_bracket():
    """The brief asks for the date to win where there is one: it stays true
    without her editing anything and a typed age does not."""
    today = date.today()
    profile = Profile(
        birth_date=date(today.year - 50, 1, 1),
        age_group="18-24",
    )
    assert group_for_profile(profile, today=date(today.year, 6, 1)) is AgeGroup.MATURE


def test_a_typed_bracket_is_used_when_there_is_no_birth_date():
    assert group_for_profile(Profile(age_group="45-54")) is AgeGroup.MATURE


# --- subtopic detection --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Новые методы лечения симптомов менопаузы", MedicalTopic.MENOPAUSE),
        ("Menopauza belgilarini yengillashtirish", MedicalTopic.MENOPAUSE),
        ("Osteoporoz va suyak zichligi", MedicalTopic.OSTEOPOROSIS),
        ("Исследование о менструальном здоровье", MedicalTopic.MENSTRUAL_HEALTH),
        ("Скрининг рака молочной железы", MedicalTopic.BREAST_HEALTH),
        ("HPV vaktsinasi va bachadon boyni saratoni", MedicalTopic.CERVICAL_HEALTH),
        ("Подростковое здоровье в школе", MedicalTopic.ADOLESCENT_HEALTH),
        ("Perimenopauza: nima kutish kerak", MedicalTopic.PERIMENOPAUSE),
        ("Женское сердечно-сосудистое здоровье", MedicalTopic.CARDIOVASCULAR),
    ],
)
def test_subtopics_are_detected_in_every_locale(text, expected):
    assert expected in detect_medical_topics(text)


def test_uzbek_apostrophes_do_not_hide_a_topic():
    """tug'ish / tugʻish / tugish are one word typed three ways."""
    assert MedicalTopic.OSTEOPOROSIS in detect_medical_topics("Suyak zichligi pasayishi")


def test_a_post_about_nothing_medical_detects_nothing():
    assert detect_medical_topics("Grant uchun ariza qabul qilinmoqda") == []


# --- the shape the brief asks for ----------------------------------------


def test_menopause_research_is_for_older_women_not_for_a_fifteen_year_old():
    """Section 03's first example."""
    got = scores("Новые методы лечения симптомов менопаузы")
    assert got[AgeGroup.TEEN.value] < 30
    assert got[AgeGroup.TEEN.value] < got[AgeGroup.YOUNG.value]
    assert got[AgeGroup.MATURE.value] > 80
    assert got[AgeGroup.MATURE.value] == max(got.values())


def test_adolescent_menstrual_health_runs_the_other_way():
    """Section 03's second example — the same axis, reversed."""
    got = scores("Новое исследование о менструальном здоровье подростков")
    assert got[AgeGroup.TEEN.value] > 80
    assert got[AgeGroup.TEEN.value] == max(got.values())
    assert got[AgeGroup.SENIOR.value] < 50


def test_a_scientific_prize_is_for_everyone():
    """Section 03's third example: nothing about it is age-bound, so the
    spread across the six brackets should be nil."""
    got = scores(
        "Женщина-учёный получила международную научную премию",
        category=NewsCategory.SCIENCE,
    )
    assert max(got.values()) - min(got.values()) == 0
    assert min(got.values()) > 80


def test_breast_cancer_research_stays_visible_to_every_bracket():
    """Section 05: medicine must not become purely demographic. A disease that
    kills women of every age cannot be scored as if it belonged to one."""
    got = scores("Новое лекарство для лечения рака молочной железы")
    assert min(got.values()) >= 55


def test_a_landmark_puts_a_floor_under_every_bracket():
    """An actual discovery outranks the demographics — but only lifts a floor,
    so the bracket it concerns still ranks highest."""
    plain = scores("Профилактика остеопороза после 60 лет")
    landmark = scores("Прорыв: впервые описан механизм остеопороза")

    assert plain[AgeGroup.TEEN.value] < IMPORTANCE_FLOOR
    assert landmark[AgeGroup.TEEN.value] >= IMPORTANCE_FLOOR
    assert landmark[AgeGroup.SENIOR.value] > landmark[AgeGroup.TEEN.value]


def test_the_floor_is_not_triggered_by_ordinary_medical_wording():
    """ "A new method" appears in half the headlines in medicine. If that were
    enough to lift the floor, the age signal would be flat everywhere."""
    got = scores("Новые методы лечения симптомов менопаузы")
    assert got[AgeGroup.TEEN.value] < IMPORTANCE_FLOOR


def test_a_post_on_two_subjects_addresses_both_audiences():
    """Taking the highest baseline per bracket rather than the average: an
    article covering periods and perimenopause is genuinely for both, and an
    average would leave it for neither."""
    got = scores("От менструального здоровья подростков до перименопаузы")
    assert got[AgeGroup.TEEN.value] > 70
    assert got[AgeGroup.MID_ADULT.value] > 70


def test_a_broad_subject_does_not_swamp_the_specific_one():
    """Almost every health post is also "about prevention", and prevention
    rises with age. Taking the maximum across every detected subject scored
    folic acid in pregnancy at 95 for a sixty-year-old — the pregnancy curve is
    the one that names the audience, so it is the one that decides."""
    got = scores("Фолиевая кислота при беременности: профилактика")
    assert got[AgeGroup.EARLY_ADULT.value] == max(got.values())
    assert got[AgeGroup.SENIOR.value] < got[AgeGroup.EARLY_ADULT.value]


def test_prevention_still_decides_when_it_is_the_actual_subject():
    """Held back, not discarded. Inside Health and Medicine a post about
    screening is a post about screening."""
    got = derive_age_relevance(
        category=NewsCategory.HEALTH,
        title_i18n={"ru": "Профилактика: 150 минут движения в неделю"},
    )
    assert got[AgeGroup.SENIOR.value] > got[AgeGroup.TEEN.value]


def test_a_science_story_that_mentions_vaccines_stays_a_science_story():
    """The Nobel for mRNA was landing on the prevention curve and losing half
    its score for a teenager. Where the editor filed the post wins when the
    only subject detected is a broad one."""
    got = derive_age_relevance(
        category=NewsCategory.SCIENCE,
        title_i18n={"ru": "Нобелевская премия за мРНК-вакцины"},
    )
    assert max(got.values()) - min(got.values()) == 0
    assert min(got.values()) > 80


# --- invariants ----------------------------------------------------------


@pytest.mark.parametrize("category", list(NewsCategory))
def test_every_category_scores_every_bracket_in_range(category):
    got = derive_age_relevance(category=category, title_i18n={"uz": "Sarlavha"})
    assert set(got) == {group.value for group in AGE_GROUPS}
    assert all(0 <= score <= 100 for score in got.values())


@pytest.mark.parametrize("topic", list(MedicalTopic))
def test_no_subtopic_ever_scores_zero_for_anyone(topic):
    """Section 08: age lowers a ranking, it never blocks. A bracket scored zero
    everywhere would be a block wearing a score's clothes."""
    got = derive_age_relevance(category=NewsCategory.HEALTH, title_i18n={"uz": "X"}, topics=[topic])
    assert min(got.values()) > 0


def test_an_unscored_post_is_neither_promoted_nor_buried():
    assert relevance_for(None, AgeGroup.TEEN, default=70) == 70
    assert relevance_for({}, AgeGroup.TEEN, default=70) == 70
    assert relevance_for({"13-17": 90}, None, default=70) == 70


def test_a_nonsense_stored_score_falls_back_instead_of_crashing():
    assert relevance_for({"13-17": "high"}, AgeGroup.TEEN, default=70) == 70
    assert relevance_for({"13-17": 900}, AgeGroup.TEEN) == 100
    assert relevance_for({"13-17": -5}, AgeGroup.TEEN) == 0
