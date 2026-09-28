"""Reading the Development Score: bands, explanations and where to act.

The score says where she stands. This module turns the number into something
the cabinet can act on — which dimensions are strong, which need attention, and
which of her own answers explain the reading. Pure functions over records the
platform already holds: no database access and no model call, so a reading is
exactly as repeatable as the score it reads.

What to *do* about a dimension is a recommendation and lives in
`services.recommendation`, which builds on the vocabularies defined here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.constants import (
    SCORE_WEIGHTS,
    DimensionBand,
    OpportunityType,
    ProgramCategory,
    ScoreDimension,
)

# A dimension at 70 or above reads as a strength; below 40 it is where to start.
STRONG_FROM = 70.0
FOCUS_BELOW = 40.0

# The diagnostic answers on a 0/25/50/75/100 scale. "Good" and "excellent"
# explain a strength, "not at all" and "very little" a gap. The middle answer
# explains neither, and is left out rather than forced into one of them.
STRENGTH_ANSWER_FROM = 75.0
WEAKNESS_ANSWER_UP_TO = 25.0

#: Programme categories that build each dimension, most direct first.
#:
#: Every category appears at least once, so no published course is out of reach
#: of the score. Order matters: the first category with a suitable programme
#: supplies the recommendation.
DIMENSION_PROGRAM_CATEGORIES: dict[ScoreDimension, tuple[ProgramCategory, ...]] = {
    ScoreDimension.EDUCATION_SKILLS: (
        ProgramCategory.VOCATIONAL_SKILLS,
        ProgramCategory.DIGITAL_SAFETY,
    ),
    ScoreDimension.EMPLOYMENT: (
        ProgramCategory.VOCATIONAL_SKILLS,
        ProgramCategory.LEGAL_LITERACY,
        ProgramCategory.MENTORSHIP_NETWORKING,
    ),
    ScoreDimension.ENTREPRENEURSHIP: (
        ProgramCategory.ENTREPRENEURSHIP,
        ProgramCategory.LEADERSHIP,
    ),
    ScoreDimension.FINANCIAL_LITERACY: (ProgramCategory.FINANCIAL_LITERACY,),
    ScoreDimension.HEALTHY_LIFESTYLE: (ProgramCategory.HEALTH,),
    ScoreDimension.FAMILY_PARENTING: (
        ProgramCategory.PARENTING,
        ProgramCategory.ETHICS_CULTURE,
    ),
    ScoreDimension.SOCIAL_ACTIVITY: (
        ProgramCategory.VOLUNTEERING,
        ProgramCategory.LEADERSHIP,
        ProgramCategory.LEGAL_LITERACY,
        ProgramCategory.MENTORSHIP_NETWORKING,
    ),
    ScoreDimension.INTERNATIONAL_INTEGRATION: (ProgramCategory.INTERNATIONAL,),
}

#: Listing types that act on each dimension. Empty where no listing moves it —
#: a healthy lifestyle is built by learning, not by an application form.
DIMENSION_OPPORTUNITY_TYPES: dict[ScoreDimension, tuple[OpportunityType, ...]] = {
    ScoreDimension.EDUCATION_SKILLS: (OpportunityType.INTERNSHIP, OpportunityType.TRAINING),
    ScoreDimension.EMPLOYMENT: (OpportunityType.VACANCY, OpportunityType.INTERNSHIP),
    ScoreDimension.ENTREPRENEURSHIP: (
        OpportunityType.GRANT,
        OpportunityType.INVESTMENT,
        OpportunityType.MARKETPLACE,
        OpportunityType.COMPETITION,
        OpportunityType.CONSULTATION,
    ),
    ScoreDimension.FINANCIAL_LITERACY: (),
    ScoreDimension.HEALTHY_LIFESTYLE: (),
    ScoreDimension.FAMILY_PARENTING: (),
    ScoreDimension.SOCIAL_ACTIVITY: (OpportunityType.MENTORSHIP,),
    ScoreDimension.INTERNATIONAL_INTEGRATION: (OpportunityType.INTERNATIONAL_PROGRAM,),
}

_CANONICAL = list(ScoreDimension)


def band_for(value: float) -> DimensionBand:
    """The band a 0-100 dimension score falls in."""
    if value >= STRONG_FROM:
        return DimensionBand.STRONG
    if value < FOCUS_BELOW:
        return DimensionBand.FOCUS
    return DimensionBand.DEVELOPING


def focus_order(scores: dict[ScoreDimension, float]) -> list[ScoreDimension]:
    """Dimensions from where acting helps most to where it helps least.

    Lowest score first. Between equal scores the dimension weighted more
    heavily in the composite comes first, because the same gain moves her
    overall score further there.
    """
    return sorted(scores, key=lambda d: (scores[d], -SCORE_WEIGHTS[d], _CANONICAL.index(d)))


@dataclass(slots=True, frozen=True)
class AnsweredQuestion:
    """One diagnostic question and the value she gave it."""

    question_id: uuid.UUID
    text_i18n: dict
    options: list
    value: float


def answer_label(options: list, value: float) -> dict:
    """The label of the option she chose, or `{}` when no option matches."""
    for option in options or []:
        try:
            if float(option["value"]) == value:
                return dict(option.get("label_i18n") or {})
        except (KeyError, TypeError, ValueError):
            continue
    return {}


def split_answers(
    answers: list[AnsweredQuestion], limit: int = 2
) -> tuple[list[AnsweredQuestion], list[AnsweredQuestion]]:
    """The answers that explain a reading: her best as strengths, her lowest as gaps."""
    strengths = sorted(
        (answer for answer in answers if answer.value >= STRENGTH_ANSWER_FROM),
        key=lambda answer: -answer.value,
    )
    weaknesses = sorted(
        (answer for answer in answers if answer.value <= WEAKNESS_ANSWER_UP_TO),
        key=lambda answer: answer.value,
    )
    return strengths[:limit], weaknesses[:limit]
