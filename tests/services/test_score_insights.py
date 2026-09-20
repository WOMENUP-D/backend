"""Reading the Development Score in words.

Pure rules, pinned without a database: the band thresholds, which dimension
counts as most in need, which answers explain a reading, which programmes may
be offered to whom, and that no programme category is out of reach of the score.
"""

import uuid

from app.core.constants import DimensionBand, ProgramCategory, ScoreDimension
from app.models.program import Program
from app.services.recommendation import program_fits
from app.services.score_insights import (
    DIMENSION_OPPORTUNITY_TYPES,
    DIMENSION_PROGRAM_CATEGORIES,
    AnsweredQuestion,
    answer_label,
    band_for,
    focus_order,
    split_answers,
)


def test_bands_follow_the_thresholds():
    assert band_for(100) is DimensionBand.STRONG
    assert band_for(70) is DimensionBand.STRONG
    assert band_for(69.9) is DimensionBand.DEVELOPING
    assert band_for(40) is DimensionBand.DEVELOPING
    assert band_for(39.9) is DimensionBand.FOCUS
    assert band_for(0) is DimensionBand.FOCUS


def test_focus_order_puts_the_lowest_first_and_breaks_ties_by_weight():
    scores = {
        ScoreDimension.SOCIAL_ACTIVITY: 30.0,  # weight 0.08
        ScoreDimension.EDUCATION_SKILLS: 30.0,  # weight 0.18
        ScoreDimension.EMPLOYMENT: 10.0,
        ScoreDimension.HEALTHY_LIFESTYLE: 80.0,
    }
    assert focus_order(scores) == [
        ScoreDimension.EMPLOYMENT,
        ScoreDimension.EDUCATION_SKILLS,
        ScoreDimension.SOCIAL_ACTIVITY,
        ScoreDimension.HEALTHY_LIFESTYLE,
    ]


def test_every_dimension_is_built_by_some_programme_category():
    assert set(DIMENSION_PROGRAM_CATEGORIES) == set(ScoreDimension)
    assert all(DIMENSION_PROGRAM_CATEGORIES.values())


def test_every_programme_category_builds_some_dimension():
    reachable = {c for categories in DIMENSION_PROGRAM_CATEGORIES.values() for c in categories}
    assert reachable == set(ProgramCategory)


def test_every_dimension_has_an_opportunity_entry():
    assert set(DIMENSION_OPPORTUNITY_TYPES) == set(ScoreDimension)


def _answer(value: float) -> AnsweredQuestion:
    return AnsweredQuestion(uuid.uuid4(), {"en": str(value)}, [], value)


def test_middle_answers_explain_nothing():
    strengths, weaknesses = split_answers([_answer(v) for v in (0, 25, 50, 75, 100)])
    assert [a.value for a in strengths] == [100, 75]
    assert [a.value for a in weaknesses] == [0, 25]


def test_explanations_are_capped():
    strengths, _ = split_answers([_answer(100) for _ in range(5)], limit=2)
    assert len(strengths) == 2


def test_answer_label_is_the_option_she_chose():
    options = [
        {"value": 0, "label_i18n": {"en": "Not at all"}},
        {"value": 75, "label_i18n": {"en": "Good"}},
    ]
    assert answer_label(options, 75.0) == {"en": "Good"}
    assert answer_label(options, 50.0) == {}
    assert answer_label([{"broken": True}, "nonsense"], 0.0) == {}


def _program(**overrides) -> Program:
    fields = {
        "slug": "course",
        "title_i18n": {},
        "category": ProgramCategory.HEALTH,
        "target_age_min": None,
        "target_age_max": None,
        "target_regions": [],
        "skills_taught": [],
    }
    return Program(**(fields | overrides))


def test_an_unknown_age_is_not_treated_as_adult():
    assert program_fits(_program(target_age_min=18), age=None, region=None) is False
    assert program_fits(_program(), age=None, region=None) is True


def test_an_age_range_is_respected_at_both_ends():
    teens = _program(target_age_min=13, target_age_max=17)
    assert program_fits(teens, age=15, region=None) is True
    assert program_fits(teens, age=12, region=None) is False
    assert program_fits(teens, age=30, region=None) is False


def test_a_region_list_restricts_only_when_her_region_is_known():
    andijan = _program(target_regions=["andijan"])
    assert program_fits(andijan, age=30, region="bukhara") is False
    assert program_fits(andijan, age=30, region="andijan") is True
    assert program_fits(andijan, age=30, region=None) is True
