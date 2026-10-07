"""Development Score computation."""

from app.core.constants import ScoreDimension
from app.services.scoring import composite_score, default_target, weakest_dimensions


def test_composite_of_uniform_scores_equals_that_score():
    scores = dict.fromkeys(ScoreDimension, 60.0)
    assert composite_score(scores) == 60.0


def test_composite_is_the_plain_mean_every_dimension_counting_the_same():
    one = composite_score({ScoreDimension.EDUCATION_SKILLS: 100.0, ScoreDimension.LEADERSHIP: 0.0})
    other = composite_score(
        {ScoreDimension.EDUCATION_SKILLS: 0.0, ScoreDimension.LEADERSHIP: 100.0}
    )
    assert one == other == 50.0


def test_partial_assessment_averages_what_is_present():
    """A single answered dimension must not be diluted by unanswered ones."""
    assert composite_score({ScoreDimension.EMPLOYMENT: 80.0}) == 80.0


def test_empty_scores_are_zero():
    assert composite_score({}) == 0.0


def test_weakest_dimensions_are_returned_lowest_first():
    scores = {
        ScoreDimension.EDUCATION_SKILLS: 90.0,
        ScoreDimension.EMPLOYMENT: 20.0,
        ScoreDimension.HEALTHY_LIFESTYLE: 45.0,
        ScoreDimension.FINANCIAL_LITERACY: 10.0,
    }
    assert weakest_dimensions(scores, limit=2) == [
        ScoreDimension.FINANCIAL_LITERACY,
        ScoreDimension.EMPLOYMENT,
    ]


def test_target_closes_part_of_the_gap_and_never_exceeds_100():
    assert default_target(40.0) == 60.0
    assert default_target(100.0) == 100.0
    assert default_target(99.0) <= 100.0
