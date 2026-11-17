"""The diagnostic's answers reach the plan and the assistant in words.

A model handed option ids ("it_programming") or blank labels could not tell
what she had said, so the plan came out generic and the assistant did not
know her interests.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.core.constants import GoalHorizon, ScoreDimension
from app.services import ai_assistant, diagnostic, plan_service

PROFILE = {
    "situation": "employed",
    "goal_primary": "change_direction",
    "goals_other": ["improve_skills"],
    "interests": ["it_programming", "data"],
    "difficulties": ["time"],
    "goal_text": "  Dasturçi bölib işlamoqçiman  ",
}


def test_profile_answers_are_told_in_words():
    lines = diagnostic.profile_lines(PROFILE)
    text = "\n".join(lines)
    assert "fields she is interested in: IT and programming, Data analytics" in text
    assert "main goal: Change career direction" in text
    assert "situation: " in text and "employed" not in text
    assert "her goal in her own words: Dasturçi bölib işlamoqçiman" in text


def test_no_profile_says_nothing():
    assert diagnostic.profile_lines(None) == []
    assert diagnostic.profile_lines({}) == []


def test_an_unknown_id_is_kept_rather_than_dropped():
    assert diagnostic.profile_lines({"interests": ["something_new"]}) == [
        "fields she is interested in: something_new"
    ]


def test_her_own_words_are_cut_short():
    line = diagnostic.profile_lines({"goal_text": "a" * 1000})[0]
    assert len(line) < diagnostic.GOAL_TEXT_LIMIT + 40


def test_the_plan_prompt_carries_what_she_said():
    prompt = plan_service._build_prompt(
        None,
        {},
        [],
        [ScoreDimension.DIGITAL_SKILLS],
        GoalHorizon.M3,
        [],
        "uz",
        about_her=diagnostic.profile_lines(PROFILE),
    )
    assert "WHAT SHE TOLD THE DIAGNOSTIC:" in prompt
    assert "- fields she is interested in: IT and programming, Data analytics" in prompt


def test_interest_courses_come_first_without_repeats():
    a, b, c = (SimpleNamespace(id=uuid.uuid4()) for _ in range(3))
    assert plan_service._with_interests([a, b], [b, c]) == [a, b, c]


def test_the_assistant_persona_remembers_the_diagnostic():
    persona = ai_assistant.Persona(
        language="uz", personalised=True, diagnostic=diagnostic.profile_lines(PROFILE)
    )
    prompt = persona.as_prompt()
    assert "what she told her diagnostic" in prompt
    assert "IT and programming" in prompt


def test_without_personalisation_the_diagnostic_is_not_told():
    persona = ai_assistant.Persona(language="uz", diagnostic=diagnostic.profile_lines(PROFILE))
    assert "IT and programming" not in persona.as_prompt()
