"""Finishing the questionnaire fills in the profile.

She answers seventeen questions about her education, her work and what she can
already do. Every one of those answers used to stop at the learning profile: her
actual profile stayed empty, the cabinet showed her a blank card, and it asked
her to type the same facts a second time.

What these tests pin down is the *restraint* of the copy, because an automatic
write into a record she also edits by hand is only safe while it cannot
overwrite her: blanks only, and never on a re-run.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Profile
from app.models.user import User
from app.services.learning_profile import fill_profile_from_answers

ANSWERS = {
    "occupation": ["working"],
    "education": ["bachelor"],
    "field": "Бухгалтерия",
    "experience": "3_5",
    "target_field": "Финансовый аналитик",
    "current_skills": "Excel, 1C",
    "tools": "Power BI; Excel",
    "language": ["uz", "ru"],
}


async def _user(session: AsyncSession) -> uuid.UUID:
    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}")
    session.add(user)
    await session.flush()
    return user.id


async def _profile(session: AsyncSession, user_id: uuid.UUID) -> Profile:
    return await session.scalar(select(Profile).where(Profile.user_id == user_id))


@pytest.mark.asyncio
async def test_the_answers_land_in_the_profile(session: AsyncSession) -> None:
    user_id = await _user(session)

    filled = await fill_profile_from_answers(session, user_id, ANSWERS)
    profile = await _profile(session, user_id)

    assert profile.employment_status == "working"
    assert profile.education_level == "bachelor"
    assert profile.education_field == "Бухгалтерия"
    assert profile.profession == "Финансовый аналитик"
    assert profile.languages == ["uz", "ru"]
    assert set(filled) >= {"employment_status", "education_level", "profession"}


@pytest.mark.asyncio
async def test_free_text_becomes_separate_skills(session: AsyncSession) -> None:
    """ "Excel, 1C" is two skills, not one tag with a comma in it. The tools
    question feeds the same list, and Excel appears in both."""
    user_id = await _user(session)

    await fill_profile_from_answers(session, user_id, ANSWERS)
    profile = await _profile(session, user_id)

    assert profile.skills == ["Excel", "1C", "Power BI"]


@pytest.mark.asyncio
async def test_an_experience_band_becomes_whole_years(session: AsyncSession) -> None:
    """The questionnaire asks for a range; the profile holds a number. The
    bottom of the band is used — understating experience is the safe direction
    when the figure ends up in front of an employer."""
    user_id = await _user(session)

    await fill_profile_from_answers(session, user_id, ANSWERS)
    profile = await _profile(session, user_id)

    assert profile.years_of_experience == 3


@pytest.mark.asyncio
async def test_nothing_she_typed_herself_is_overwritten(session: AsyncSession) -> None:
    """A profile she has edited outranks a questionnaire she may have run months
    ago. Re-taking the assessment must not quietly rewrite it."""
    user_id = await _user(session)
    session.add(
        Profile(
            user_id=user_id,
            profession="Главный бухгалтер",
            education_level="master",
            skills=["SAP"],
            years_of_experience=9,
        )
    )
    await session.flush()

    filled = await fill_profile_from_answers(session, user_id, ANSWERS)
    profile = await _profile(session, user_id)

    assert profile.profession == "Главный бухгалтер"
    assert profile.education_level == "master"
    assert profile.skills == ["SAP"]
    assert profile.years_of_experience == 9
    # The blanks it *could* fill are still filled.
    assert profile.employment_status == "working"
    assert "profession" not in filled


@pytest.mark.asyncio
async def test_running_it_twice_changes_nothing(session: AsyncSession) -> None:
    user_id = await _user(session)

    await fill_profile_from_answers(session, user_id, ANSWERS)
    second = await fill_profile_from_answers(session, user_id, ANSWERS)

    assert second == []


@pytest.mark.asyncio
async def test_a_half_finished_questionnaire_fills_only_what_it_has(
    session: AsyncSession,
) -> None:
    """She can leave and come back, so partial answers are normal. The fields
    she has not reached stay empty rather than being written as blanks."""
    user_id = await _user(session)

    await fill_profile_from_answers(session, user_id, {"occupation": ["studying"]})
    profile = await _profile(session, user_id)

    assert profile.employment_status == "studying"
    assert profile.profession is None
    assert profile.skills == []
