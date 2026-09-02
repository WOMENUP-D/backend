"""Skill matching."""

from app.services.recommendation import skill_overlap


def test_matching_is_case_and_whitespace_insensitive():
    matched, missing, coverage = skill_overlap(["  Python ", "SMM"], ["python", "smm"])
    assert sorted(matched) == ["python", "smm"]
    assert missing == []
    assert coverage == 1.0


def test_missing_skills_are_reported():
    matched, missing, coverage = skill_overlap(["excel"], ["excel", "1c", "english"])
    assert matched == ["excel"]
    assert sorted(missing) == ["1c", "english"]
    assert coverage == round(1 / 3, 3)


def test_opportunity_without_requirements_is_fully_covered():
    assert skill_overlap([], []) == ([], [], 1.0)
