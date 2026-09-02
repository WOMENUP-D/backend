"""The questionnaire's shape, and what `derive` makes of several answers.

Two things are pinned here. First, that a question which can truthfully have
more than one answer accepts more than one — a woman on maternity leave is
often also studying and also looking for work, and making her delete two true
answers is how a profile ends up wrong. Second, that answers saved under the
older single-choice wording keep meaning what they meant.
"""

from __future__ import annotations

from app.services import questionnaire
from app.services.learning_profile import derive, missing_required

MULTI = {"occupation", "education", "goal", "task_style", "language"}


def by_id(qid: str) -> dict:
    return next(q for q in questionnaire.QUESTIONS if q["id"] == qid)


def test_questions_that_overlap_in_real_life_accept_several_answers():
    for qid in MULTI:
        assert by_id(qid)["type"] == "multi", qid


def test_bands_and_ladders_stay_single():
    """Ranges and rungs are exclusive by construction: no experience *and*
    five years, or no practice *and* commercial work, is not an answer."""
    for qid in ("experience", "hours_per_week", "practice"):
        assert by_id(qid)["type"] == "single", qid


def test_every_open_question_offers_ready_answers():
    """A blank box is where this questionnaire gets abandoned."""
    for question in questionnaire.QUESTIONS:
        if question["type"] == "text":
            assert question.get("suggestions_i18n"), question["id"]
            assert question["suggest_mode"] in {"set", "add"}


def test_suggestions_are_translated_into_all_three_languages():
    for question in questionnaire.QUESTIONS:
        for item in question.get("suggestions_i18n") or []:
            assert set(item) == {"uz", "ru", "en"}, question["id"]
            assert all(text.strip() for text in item.values()), question["id"]


def test_the_field_she_wants_to_learn_is_asked_once():
    """It used to be asked twice — once as free text, once as a list of areas."""
    ids = [q["id"] for q in questionnaire.QUESTIONS]
    assert "target_field" in ids
    assert "interest_areas" not in ids


def test_several_answers_survive_into_the_summary():
    summary = derive(
        {
            "occupation": ["maternity", "study"],
            "education": ["bachelor", "studying_now"],
            "goal": ["find_job", "income"],
            "language": ["uz", "ru"],
        }
    )
    assert summary["occupations"] == ["maternity", "study"]
    assert summary["educations"] == ["bachelor", "studying_now"]
    assert summary["goals"] == ["find_job", "income"]
    assert summary["languages"] == ["uz", "ru"]
    # The level test asks in one language, so one of them has to lead.
    assert summary["language"] == "uz"


def test_an_answer_saved_under_the_old_wording_still_reads_correctly():
    summary = derive({"occupation": "study", "education": "bachelor", "language": "ru"})
    assert summary["occupations"] == ["study"]
    assert summary["educations"] == ["bachelor"]
    assert summary["language"] == "ru"
    assert summary["languages"] == ["ru"]


def test_a_missing_language_falls_back_rather_than_breaking_the_test():
    assert derive({})["language"] == "uz"


def test_interests_fall_back_to_the_field_she_wants_to_learn():
    """The question that used to fill them is gone; the answer is still there."""
    assert derive({"target_field": "Дизайн"})["interests"] == ["Дизайн"]
    # An older answer set still carries its own.
    assert derive({"interest_areas": ["it"], "target_field": "Дизайн"})["interests"] == ["it"]


def test_an_empty_multi_answer_counts_as_unanswered():
    missing = missing_required({qid: [] for qid in questionnaire.required_ids()})
    assert set(missing) == questionnaire.required_ids()


def test_the_version_moved_with_the_wording():
    """`version` is stamped on every completed run so an old answer set stays
    identifiable after questions change shape."""
    assert questionnaire.VERSION == 2
