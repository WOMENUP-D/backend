"""Diagnostic v2: scoring, validation and priorities — pure, no database."""

import uuid

import pytest

from app.core.constants import ScoreDimension
from app.models.assessment import AssessmentQuestion
from app.services import diagnostic
from app.services.diagnostic import DiagnosticError

D = ScoreDimension


def _questions() -> list[AssessmentQuestion]:
    rows = diagnostic.question_rows(diagnostic.load_bank())
    return [AssessmentQuestion(id=uuid.uuid4(), **row) for row in rows]


QUESTIONS = _questions()
BY_CODE = {q.code: q for q in QUESTIONS}


def _answers(
    scored: str | dict[str, str] = "c", *, family: str = "a", goals: list[str] | None = None
) -> list[tuple[uuid.UUID, list[str]]]:
    """Every question answered. `scored` is one letter for all scored
    questions, or a {code: letter} map over a default of "c"."""
    out = []
    for q in QUESTIONS:
        if q.question_type == "goals":
            out.append((q.id, goals if goals is not None else ["find_job"]))
        elif q.question_type == "routing":
            out.append((q.id, [family]))
        elif isinstance(scored, str):
            out.append((q.id, [scored]))
        else:
            out.append((q.id, [scored.get(q.code, "c")]))
    return out


def _scores(answers) -> dict[ScoreDimension, int]:
    return diagnostic.dimension_scores(diagnostic.validate(QUESTIONS, answers))


# --- the instrument ---------------------------------------------------------------


def test_the_bank_has_25_questions_three_per_dimension_and_two_steering_ones():
    assert len(QUESTIONS) == 25
    scored = [q for q in QUESTIONS if q.question_type == "single_choice"]
    assert len(scored) == 23  # family has two scored questions plus the routing one
    for dimension in ScoreDimension:
        expected = 2 if dimension == D.FAMILY_PARENTING else 3
        assert sum(q.dimension == dimension for q in scored) == expected
    assert BY_CODE["fam.q19"].question_type == "routing"
    assert BY_CODE["fam.q19"].weight == 0
    assert BY_CODE["goals.q25"].question_type == "goals"
    assert BY_CODE["goals.q25"].dimension is None
    assert BY_CODE["goals.q25"].meta["max_choices"] == 3


def test_every_question_and_option_is_written_in_every_language():
    for q in QUESTIONS:
        assert set(q.text_i18n) == {"uz", "ru", "en"}
        for option in q.options:
            assert set(option["label_i18n"]) == {"uz", "ru", "en"}
            assert all(option["label_i18n"].values())


def test_scored_options_are_worth_0_to_4_in_order():
    for q in QUESTIONS:
        if q.question_type != "single_choice":
            continue
        assert [o["id"] for o in q.options] == list("abcde")
        assert [o["score"] for o in q.options] == [0, 1, 2, 3, 4]
        assert [o["value"] for o in q.options] == [0, 25, 50, 75, 100]


# --- scoring ----------------------------------------------------------------------


def test_all_lowest_answers_score_zero_everywhere():
    scores = _scores(_answers("a"))
    assert scores == dict.fromkeys(ScoreDimension, 0)
    assert diagnostic.overall_score(scores) == 0


def test_all_highest_answers_score_100_everywhere():
    scores = _scores(_answers("e"))
    assert scores == dict.fromkeys(ScoreDimension, 100)
    assert diagnostic.overall_score(scores) == 100


def test_an_average_run_scores_50():
    scores = _scores(_answers("c"))
    assert scores == dict.fromkeys(ScoreDimension, 50)
    assert diagnostic.overall_score(scores) == 50


@pytest.mark.parametrize(
    ("dimension", "codes"),
    [
        (D.EDUCATION_SKILLS, ("edu.q1", "edu.q2", "edu.q3")),
        (D.EMPLOYMENT, ("car.q4", "car.q5", "car.q6")),
        (D.ENTREPRENEURSHIP, ("ent.q7", "ent.q8", "ent.q9")),
        (D.FINANCIAL_LITERACY, ("fin.q10", "fin.q11", "fin.q12")),
        (D.DIGITAL_SKILLS, ("dig.q13", "dig.q14", "dig.q15")),
        (D.HEALTHY_LIFESTYLE, ("hea.q16", "hea.q17", "hea.q18")),
        (D.LEADERSHIP, ("lea.q22", "lea.q23", "lea.q24")),
    ],
)
def test_each_dimension_is_the_sum_of_its_three_answers_over_12(dimension, codes):
    # e (4) + d (3) + b (1) = 8 → 8 / 12 * 100 = 66.67 → 67
    scores = _scores(_answers(dict(zip(codes, "edb", strict=True))))
    assert scores[dimension] == 67
    assert all(v == 50 for d, v in scores.items() if d != dimension)


def test_family_is_scored_on_its_two_questions_and_ignores_the_routing_one():
    # Q20 = e (4), Q21 = d (3) → 7 / 8 * 100 = 87.5 → 88 (half up)
    for family in "abcdef":
        scores = _scores(_answers({"fam.q20": "e", "fam.q21": "d"}, family=family))
        assert scores[D.FAMILY_PARENTING] == 88


def test_goals_never_change_the_score():
    one = _scores(_answers(goals=["find_job"]))
    other = _scores(_answers(goals=["business", "wellbeing", "leadership"]))
    assert one == other


def test_overall_is_the_rounded_mean_of_the_eight():
    scores = dict.fromkeys(ScoreDimension, 50)
    scores[D.EDUCATION_SKILLS] = 54  # mean 50.5 → 51
    assert diagnostic.overall_score(scores) == 51


def test_rounding_is_half_up():
    assert diagnostic.round_half_up(62.5) == 63
    assert diagnostic.round_half_up(62.49) == 62


@pytest.mark.parametrize(
    ("score", "level"),
    [
        (0, "starting_point"),
        (24, "starting_point"),
        (25, "building_foundation"),
        (49, "building_foundation"),
        (50, "good_foundation"),
        (74, "good_foundation"),
        (75, "strong_area"),
        (89, "strong_area"),
        (90, "advanced"),
        (100, "advanced"),
    ],
)
def test_levels(score, level):
    assert diagnostic.level_for(score) == level


# --- validation -------------------------------------------------------------------


def test_an_unknown_question_is_refused():
    answers = _answers() + [(uuid.uuid4(), ["a"])]
    with pytest.raises(DiagnosticError) as error:
        diagnostic.validate(QUESTIONS, answers)
    assert error.value.code == "invalid_question"


def test_an_option_the_question_does_not_have_is_refused():
    answers = _answers()
    answers[0] = (answers[0][0], ["z"])
    with pytest.raises(DiagnosticError) as error:
        diagnostic.validate(QUESTIONS, answers)
    assert error.value.code == "invalid_answer"
    assert error.value.items == ["edu.q1"]


def test_two_options_on_a_single_choice_question_are_refused():
    answers = _answers()
    answers[0] = (answers[0][0], ["a", "b"])
    with pytest.raises(DiagnosticError) as error:
        diagnostic.validate(QUESTIONS, answers)
    assert error.value.code == "invalid_answer"


def test_answering_a_question_twice_is_refused():
    answers = _answers()
    answers.append(answers[0])
    with pytest.raises(DiagnosticError) as error:
        diagnostic.validate(QUESTIONS, answers)
    assert error.value.code == "duplicate_answer"


def test_an_incomplete_run_names_what_is_missing():
    answers = [a for a in _answers() if a[0] not in {BY_CODE["edu.q2"].id, BY_CODE["lea.q24"].id}]
    with pytest.raises(DiagnosticError) as error:
        diagnostic.validate(QUESTIONS, answers)
    assert error.value.code == "incomplete"
    assert error.value.items == ["edu.q2", "lea.q24"]


def test_at_most_three_goals():
    with pytest.raises(DiagnosticError) as error:
        diagnostic.validate(
            QUESTIONS, _answers(goals=["find_job", "business", "family", "wellbeing"])
        )
    assert error.value.code == "too_many_goals"
    diagnostic.validate(QUESTIONS, _answers(goals=["find_job", "business", "family"]))


def test_an_unknown_goal_is_refused():
    with pytest.raises(DiagnosticError) as error:
        diagnostic.validate(QUESTIONS, _answers(goals=["become_famous"]))
    assert error.value.code == "invalid_answer"


# --- priorities -------------------------------------------------------------------


def test_goal_match_is_direct_indirect_or_none():
    assert diagnostic.goal_match(D.EMPLOYMENT, ["find_job"]) == 100
    assert diagnostic.goal_match(D.DIGITAL_SKILLS, ["find_job"]) == 50
    assert diagnostic.goal_match(D.HEALTHY_LIFESTYLE, ["find_job"]) == 0
    # Direct wins over indirect when two goals disagree.
    assert diagnostic.goal_match(D.EDUCATION_SKILLS, ["find_job", "learn_skills"]) == 100


def test_urgency_reads_only_explicit_status_answers():
    assert diagnostic.urgency(D.EMPLOYMENT, {"car.q4": ["a"]}) == 100
    assert diagnostic.urgency(D.EMPLOYMENT, {"car.q4": ["e"]}) == 50
    assert diagnostic.urgency(D.FINANCIAL_LITERACY, {"fin.q11": ["a"]}) == 75
    # Nothing medical or sensitive is ever made urgent.
    assert diagnostic.urgency(D.HEALTHY_LIFESTYLE, {"hea.q17": ["a"]}) == 50
    assert diagnostic.urgency(D.FAMILY_PARENTING, {}) == 50


def test_priority_is_the_weighted_sum_of_its_four_parts():
    scores = dict.fromkeys(ScoreDimension, 50)
    scores[D.EMPLOYMENT] = 25
    ranked = diagnostic.priorities(
        scores, ["find_job"], {"car.q4": ["a"]}, {D.EMPLOYMENT: 80.0}, family_focus="a"
    )
    career = next(p for p in ranked if p.dimension == D.EMPLOYMENT)
    # 0.4 * 75 + 0.3 * 100 + 0.2 * 80 + 0.1 * 100 = 30 + 30 + 16 + 10
    assert career.priority == 86.0
    assert (career.need, career.goal_match, career.skill_gap, career.urgency) == (75, 100, 80, 100)
    assert ranked[0].dimension == D.EMPLOYMENT


def test_missing_skill_data_is_a_neutral_50():
    ranked = diagnostic.priorities(dict.fromkeys(ScoreDimension, 50), [], {}, {}, "a")
    assert all(p.skill_gap == 50 for p in ranked)
    # Need 50, no goal, gap 50, urgency 50 → 20 + 0 + 10 + 5
    assert all(p.priority == 35.0 for p in ranked)


def test_a_goal_can_outrank_a_lower_score():
    scores = dict.fromkeys(ScoreDimension, 60)
    scores[D.HEALTHY_LIFESTYLE] = 40  # lowest, but not what she wants to change
    ranked = diagnostic.priorities(scores, ["business"], {}, {}, "a")
    # ENT: 0.4*40 + 0.3*100 + 10 + 5 = 61; HEA: 0.4*60 + 0 + 10 + 5 = 39
    assert ranked[0].dimension == D.ENTREPRENEURSHIP
    assert ranked[0].dimension != min(scores, key=scores.get)


def test_family_is_left_out_when_she_says_it_is_not_relevant():
    scores = dict.fromkeys(ScoreDimension, 70)
    scores[D.FAMILY_PARENTING] = 0
    ranked = diagnostic.priorities(scores, [], {}, {}, family_focus="f")
    family = next(p for p in ranked if p.dimension == D.FAMILY_PARENTING)
    assert family.eligible is False
    assert ranked[-1] is family
    # Unless she picked family as a goal after all.
    ranked = diagnostic.priorities(scores, ["family"], {}, {}, family_focus="f")
    assert ranked[0].dimension == D.FAMILY_PARENTING
    assert ranked[0].eligible


def test_strongest_are_the_two_highest_in_canonical_order_on_ties():
    scores = dict.fromkeys(ScoreDimension, 50)
    scores[D.LEADERSHIP] = 90
    scores[D.DIGITAL_SKILLS] = 90
    assert diagnostic.strongest(scores) == [D.DIGITAL_SKILLS, D.LEADERSHIP]


def test_a_growth_area_is_never_also_listed_as_a_strength():
    scores = dict.fromkeys(ScoreDimension, 25)
    growth = {D.EMPLOYMENT, D.EDUCATION_SKILLS, D.LEADERSHIP}
    assert diagnostic.strongest(scores, exclude=growth) == [
        D.ENTREPRENEURSHIP,
        D.FINANCIAL_LITERACY,
    ]
