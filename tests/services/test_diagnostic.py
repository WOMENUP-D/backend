"""Diagnostic v3: scoring, validation, branching and priorities — pure, no database."""

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
    scored: str | dict[str, str] = "c",
    *,
    family: str = "a",
    goals: list[str] | None = None,
    profile: dict[str, list[str]] | None = None,
    text: dict[str, str] | None = None,
) -> list[tuple[uuid.UUID, list[str]]]:
    """Every question answered. `scored` is one letter for all scored
    questions, or a {code: letter} map over a default of "c". `goals` sets
    the version-3 main goal (the first one)."""
    overrides = dict(profile or {})
    if goals is not None:
        overrides["pro.q3"] = goals[:1]
    return diagnostic.sample_answers(
        QUESTIONS, scored=scored, family=family, profile=overrides, text=text
    )


def _scores(answers) -> dict[ScoreDimension, int]:
    return diagnostic.dimension_scores(diagnostic.validate(QUESTIONS, answers))


# --- the instrument ---------------------------------------------------------------


def test_the_bank_opens_with_the_profile_and_scores_two_questions_per_dimension():
    # Version 4 (QA audit D-07): two self-ratings per dimension, the second
    # taken from version 2, plus the "other goals" question (D-04).
    assert len(QUESTIONS) == 35
    scored = [q for q in QUESTIONS if q.question_type == "single_choice" and q.dimension]
    assert len(scored) == 16
    for dimension in ScoreDimension:
        assert sum(q.dimension == dimension for q in scored) == 2
    assert BY_CODE["fam.q19"].question_type == "routing"
    assert BY_CODE["fam.q19"].weight == 0
    # The profile block: her situation, her goals, her interests — no score.
    assert BY_CODE["pro.q1"].question_type == "single_choice"
    assert BY_CODE["pro.q1"].dimension is None and BY_CODE["pro.q1"].weight == 0
    assert BY_CODE["pro.q3"].meta["section"] == "goals"
    assert BY_CODE["pro.q6"].question_type == "multi_choice"
    assert BY_CODE["pro.q6"].meta["max_choices"] == 3
    assert BY_CODE["pro.q5"].question_type == "text" and BY_CODE["pro.q5"].meta["optional"]
    # Twelve questions depend on earlier answers; the profile comes first.
    assert sum(1 for q in QUESTIONS if q.meta.get("show_if")) == 12
    assert [q.code for q in QUESTIONS][:5] == ["pro.q1", "pro.q3", "pro.q4", "pro.q5", "pro.q6"]


def test_every_question_and_option_is_written_in_every_language():
    for q in QUESTIONS:
        assert set(q.text_i18n) == {"uz", "ru", "en"}
        for option in q.options:
            assert set(option["label_i18n"]) == {"uz", "ru", "en"}
            assert all(option["label_i18n"].values())


def test_scored_options_are_worth_0_to_4_in_order():
    for q in QUESTIONS:
        if q.question_type != "single_choice" or q.dimension is None:
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
    ("dimension", "code", "second"),
    [
        (D.EDUCATION_SKILLS, "edu.q1", "edu.q3"),
        (D.EMPLOYMENT, "car.q5", "car.q6"),
        (D.ENTREPRENEURSHIP, "ent.q8", "ent.q7"),
        (D.FINANCIAL_LITERACY, "fin.q10", "fin.q11"),
        (D.DIGITAL_SKILLS, "dig.q13", "dig.q14"),
        (D.HEALTHY_LIFESTYLE, "hea.q17", "hea.q18"),
        (D.LEADERSHIP, "lea.q22", "lea.q23"),
    ],
)
def test_each_dimension_is_its_two_answers_over_8(dimension, code, second):
    # Version 4 (QA audit D-07): two self-ratings per dimension, so one
    # click moves it 12.5 points, not 25. b (1) + c (2) → 3 / 8 → 37.5 → 38.
    assert _scores(_answers({code: "b"}))[dimension] == 38
    assert _scores(_answers({code: "b", second: "b"}))[dimension] == 25
    scores = _scores(_answers({code: "e", second: "e"}))
    assert scores[dimension] == 100
    assert all(v == 50 for d, v in scores.items() if d != dimension)


def test_family_is_scored_on_its_question_and_ignores_the_routing_one():
    for family in "abcdef":
        scores = _scores(_answers({"fam.q20": "d", "fam.q21": "d"}, family=family))
        assert scores[D.FAMILY_PARENTING] == 75


def test_goals_and_the_profile_never_change_the_score():
    one = _scores(_answers(goals=["first_job"]))
    other = _scores(_answers(goals=["start_business", "international", "communication_leadership"]))
    student = _scores(_answers(profile={"pro.q1": ["university_student"], "sit.q7": ["it"]}))
    assert one == other == student


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
    assert error.value.items == ["pro.q1"]


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
    answers = [a for a in _answers() if a[0] not in {BY_CODE["edu.q1"].id, BY_CODE["lea.q22"].id}]
    with pytest.raises(DiagnosticError) as error:
        diagnostic.validate(QUESTIONS, answers)
    assert error.value.code == "incomplete"
    assert error.value.items == ["edu.q1", "lea.q22"]


def test_at_most_three_interests():
    four = ["it_programming", "ai", "data", "business"]
    with pytest.raises(DiagnosticError) as error:
        diagnostic.validate(QUESTIONS, _answers(profile={"pro.q6": four}))
    assert error.value.code == "too_many_goals"
    diagnostic.validate(QUESTIONS, _answers(profile={"pro.q6": four[:3]}))


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
    # car.q5 since version 3 asks no car.q4 (QA audit D-07).
    assert diagnostic.urgency(D.EMPLOYMENT, {"car.q5": ["a"]}) == 100
    assert diagnostic.urgency(D.EMPLOYMENT, {"car.q5": ["e"]}) == 50
    assert diagnostic.urgency(D.EMPLOYMENT, {"car.q4": ["a"]}) == 50
    assert diagnostic.urgency(D.FINANCIAL_LITERACY, {"fin.q10": ["a"]}) == 75
    # Nothing medical or sensitive is ever made urgent.
    assert diagnostic.urgency(D.HEALTHY_LIFESTYLE, {"hea.q17": ["a"]}) == 50
    assert diagnostic.urgency(D.FAMILY_PARENTING, {}) == 50


def test_priority_is_the_weighted_sum_of_its_four_parts():
    scores = dict.fromkeys(ScoreDimension, 50)
    scores[D.EMPLOYMENT] = 25
    ranked = diagnostic.priorities(
        scores, ["find_job"], {"car.q5": ["a"]}, {D.EMPLOYMENT: 80.0}, family_focus="a"
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
    scores = dict.fromkeys(ScoreDimension, 50)
    growth = {D.EMPLOYMENT, D.EDUCATION_SKILLS, D.LEADERSHIP}
    assert diagnostic.strongest(scores, exclude=growth) == [
        D.ENTREPRENEURSHIP,
        D.FINANCIAL_LITERACY,
    ]


# --- version 3: branching and the profile -----------------------------------------


def _shown(answers) -> set[str]:
    return {q.code for q in diagnostic.validate(QUESTIONS, answers).questions}


def test_a_student_and_an_entrepreneur_are_asked_different_questions():
    student = _shown(_answers(profile={"pro.q1": ["university_student"], "sit.q7": ["it"]}))
    assert {"sit.q7", "sit.q9"} <= student
    assert not ({"sit.q10", "sit.q13", "sit.q14", "sit.q15"} & student)
    owner = _shown(_answers(profile={"pro.q1": ["entrepreneur"], "sit.q13": ["idea"]}))
    assert {"sit.q13", "sit.q14"} <= owner and "sit.q7" not in owner


def test_a_skill_question_follows_the_interest_or_the_goal():
    coder = _shown(_answers(profile={"pro.q6": ["it_programming"]}))
    assert "skl.q16" in coder and "skl.q18" not in coder
    # English is asked for the "international" goal even without a language interest.
    abroad = _shown(_answers(profile={"pro.q6": ["design"], "pro.q3": ["international"]}))
    assert "skl.q19" in abroad


def test_an_answer_to_a_hidden_question_is_dropped_and_a_hidden_one_is_not_required():
    answers = _answers(profile={"pro.q1": ["employed"], "sit.q7": ["it"]})  # sit.q7 is a student's
    submission = diagnostic.validate(QUESTIONS, answers)
    assert BY_CODE["sit.q7"].id not in submission.chosen
    without = [a for a in answers if a[0] != BY_CODE["sit.q13"].id]
    diagnostic.validate(QUESTIONS, without)  # the business stage is nobody's but an entrepreneur's


def test_optional_questions_may_be_left_out_and_text_is_kept_trimmed():
    answers = [a for a in _answers() if a[0] != BY_CODE["pro.q5"].id]
    diagnostic.validate(QUESTIONS, answers)
    told = _answers(text={"pro.q5": "  Найти работу аналитиком данных  "})
    profile = diagnostic.profile_from(diagnostic.validate(QUESTIONS, told))
    assert profile["goal_text"] == "Найти работу аналитиком данных"
    assert "goal_text" not in diagnostic.profile_from(diagnostic.validate(QUESTIONS, _answers()))


def test_the_profile_is_what_she_said_about_herself():
    answers = _answers(
        goals=["first_job", "improve_skills", "international"],
        profile={
            "pro.q1": ["graduate"],
            "pro.q6": ["it_programming", "languages"],
            "sit.q9": ["now"],
            "skl.q16": ["l2"],
            "skl.q19": ["l3"],
            "pro.q21": ["cv", "lack_experience"],
        },
    )
    profile = diagnostic.profile_from(diagnostic.validate(QUESTIONS, answers))
    assert profile["situation"] == "graduate"
    assert profile["goal_primary"] == "first_job"
    assert profile["goals"] == ["first_job"]
    assert profile["interests"] == ["it_programming", "languages"]
    assert profile["first_job_when"] == "now"
    assert profile["skill_levels"] == {"it": 2, "english": 3}
    assert profile["difficulties"] == ["cv", "lack_experience"]
    assert "hours" not in profile and "language" not in profile
    assert diagnostic.legacy_goals(profile) == ["find_job"]


def test_the_situation_sets_how_pressing_employment_is():
    assert diagnostic.urgency(D.EMPLOYMENT, {}, {"situation": "job_seeker"}) == 100
    assert diagnostic.urgency(D.EMPLOYMENT, {}, {"situation": "graduate"}) == 75
    assert diagnostic.urgency(D.EMPLOYMENT, {}, {"situation": "employed"}) == 50
    # Other dimensions keep their own rule.
    assert (
        diagnostic.urgency(D.FINANCIAL_LITERACY, {"fin.q10": ["a"]}, {"situation": "job_seeker"})
        == 75
    )


# --- QA audit -------------------------------------------------------------------


def test_a_zero_is_never_a_strength():
    """D-03: with every dimension at 0 there is no strength to show."""
    assert diagnostic.strongest(dict.fromkeys(ScoreDimension, 0)) == []
    scores = dict.fromkeys(ScoreDimension, 25)
    scores[D.HEALTHY_LIFESTYLE] = 50
    assert diagnostic.strongest(scores) == [D.HEALTHY_LIFESTYLE]


def test_other_goals_are_collected_without_the_main_one():
    """D-04: the hint promised a question about further goals; now it exists."""
    answers = _answers(
        goals=["start_business"],
        profile={"pro.q4": ["start_business", "financial_independence"]},
    )
    profile = diagnostic.profile_from(diagnostic.validate(QUESTIONS, answers))
    assert profile["goals"] == ["start_business", "financial_independence"]
    assert profile["goals_other"] == ["financial_independence"]


def test_a_mother_at_home_is_not_asked_how_long_she_has_looked_for_work():
    """D-08: "at home" and "on maternity leave" are situations of their own."""
    options = {o["id"] for o in BY_CODE["pro.q1"].options}
    assert {"home", "maternity"} <= options
    for situation in ("home", "maternity"):
        by_code = {"pro.q1": [situation]}
        assert not diagnostic.visible(BY_CODE["sit.q15"], by_code)
        profile = {"situation": situation}
        assert diagnostic.urgency(D.EMPLOYMENT, {}, profile) == diagnostic.NEUTRAL


def test_the_texts_promise_what_the_bank_does():
    """D-10, D-15: three interests, not four; grammatical Uzbek; no dead show_if."""
    assert BY_CODE["pro.q6"].meta["max_choices"] == 3
    assert "3" in BY_CODE["pro.q6"].text_i18n["uz"]
    assert "three" in BY_CODE["pro.q6"].text_i18n["en"]
    assert BY_CODE["sit.q15"].text_i18n["uz"] == "Qança vaqtdan beri iş qidiryapsiz?"
    interests = {o["id"] for o in BY_CODE["pro.q6"].options}
    for rule in BY_CODE["skl.q19"].meta["show_if"]["any"]:
        if rule["question"] == "pro.q6":
            assert set(rule["in"]) <= interests


def test_every_uzbek_text_is_in_the_reformed_alphabet():
    from app.services.uzbek import to_new_alphabet

    for q in QUESTIONS:
        assert to_new_alphabet(q.text_i18n["uz"]) == q.text_i18n["uz"]
        for option in q.options:
            assert to_new_alphabet(option["label_i18n"]["uz"]) == option["label_i18n"]["uz"]


def test_an_international_goal_points_at_education_not_leadership():
    """D-15c."""
    assert diagnostic.goal_match(D.EDUCATION_SKILLS, ["international"]) == 100
    assert diagnostic.goal_match(D.LEADERSHIP, ["international"]) == 50
