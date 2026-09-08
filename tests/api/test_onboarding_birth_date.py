"""Sign-up asks for a date of birth, and the safety band follows from it.

These are safety tests. The portal is open to girls from ten, and every case
below is one where the wrong answer puts adult reproductive content in front of
a child, or ages a seventeen-year-old into the adult band before her birthday.

The bug they exist to prevent is specific and was live: onboarding stored
`date(today.year - age, 1, 1)`, so a girl who typed 17 in December was 18 to
the gate on 1 January — up to eleven months early, silently, with no way for her
to notice or correct it.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import AgeBand
from app.models.profile import Profile
from app.models.user import User
from app.services.age_gate import age_from_profile, band_for_profile

pytestmark = pytest.mark.asyncio


def _born(years: int, *, today: date | None = None) -> str:
    """An ISO birth date for someone exactly `years` old today."""
    today = today or date.today()
    return date(today.year - years, today.month, min(today.day, 28)).isoformat()


async def _account(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = User(id=user_id, phone=f"+9989{user_id.int % 10**8:08d}")
    session.add(user)
    await session.flush()
    return user


async def _profile(session: AsyncSession, user_id: uuid.UUID) -> Profile:
    return await session.scalar(select(Profile).where(Profile.user_id == user_id))


async def _onboard(client: AsyncClient, headers: dict[str, str], **payload) -> int:
    body = {"name": "Dilnoza", "consent_ai_personalisation": True, **payload}
    response = await client.post("/api/v1/assistant/onboarding", json=body, headers=headers)
    return response.status_code


async def test_a_given_date_is_stored_exactly_as_given(
    client: AsyncClient, session: AsyncSession, user_id: uuid.UUID, auth_headers: dict[str, str]
) -> None:
    await _account(session, user_id)
    assert await _onboard(client, auth_headers, birth_date="2004-03-17") == 200

    profile = await _profile(session, user_id)
    assert profile.birth_date == date(2004, 3, 17)


async def test_a_ten_year_old_lands_in_the_child_band(
    client: AsyncClient, session: AsyncSession, user_id: uuid.UUID, auth_headers: dict[str, str]
) -> None:
    """The case the whole age gate exists for.

    `AgeGroup`'s youngest bracket starts at 13, so anything that routes a
    ten-year-old's band through the bracket string reads her back as 13 and
    opens the teenage health scope to her. The band must come from the date.
    """
    await _account(session, user_id)
    assert await _onboard(client, auth_headers, birth_date=_born(10)) == 200

    profile = await _profile(session, user_id)
    assert age_from_profile(profile) == 10
    assert band_for_profile(profile) is AgeBand.CHILD


async def test_a_legacy_age_still_lands_in_the_child_band(
    client: AsyncClient, session: AsyncSession, user_id: uuid.UUID, auth_headers: dict[str, str]
) -> None:
    """A client that was open across the deploy still sends `age`.

    It must not be quietly less protective than the new field. Storing only the
    editorial bracket would read 10 back as 13.
    """
    await _account(session, user_id)
    assert await _onboard(client, auth_headers, age=10) == 200

    profile = await _profile(session, user_id)
    assert age_from_profile(profile) == 10
    assert band_for_profile(profile) is AgeBand.CHILD


async def test_a_stated_age_is_anchored_to_today_not_to_january(
    client: AsyncClient, session: AsyncSession, user_id: uuid.UUID, auth_headers: dict[str, str]
) -> None:
    """The anchor must be the latest date consistent with what she said.

    She turns `age + 1` a full year from today, not on the next 1 January. For a
    seventeen-year-old that difference is the adult health gate.
    """
    await _account(session, user_id)
    assert await _onboard(client, auth_headers, age=17) == 200

    profile = await _profile(session, user_id)
    today = date.today()
    assert age_from_profile(profile, today=today) == 17
    # One day before the anniversary she is still seventeen.
    assert age_from_profile(profile, today=date(today.year + 1, today.month, 1)) == 17
    assert band_for_profile(profile, today=date(today.year + 1, today.month, 1)) is AgeBand.TEEN


@pytest.mark.parametrize(
    "payload",
    [
        {"birth_date": "2099-01-01"},  # the future
        {"birth_date": _born(9)},  # below the supported age
        {"birth_date": _born(140)},  # a slipped digit
        {},  # neither a date nor an age
    ],
)
async def test_an_unusable_answer_is_refused(
    client: AsyncClient,
    session: AsyncSession,
    user_id: uuid.UUID,
    auth_headers: dict[str, str],
    payload: dict,
) -> None:
    """Refused rather than defaulted.

    Inventing an age for an empty answer is the failure mode worth naming: an
    unknown age is treated as a minor everywhere else, and a silent default
    would route a child straight past that.
    """
    await _account(session, user_id)
    assert await _onboard(client, auth_headers, **payload) == 422


async def test_coming_back_to_correct_a_date_does_not_erase_her_interests(
    client: AsyncClient, session: AsyncSession, user_id: uuid.UUID, auth_headers: dict[str, str]
) -> None:
    """This endpoint is a re-entry point, linked from the cabinet.

    Its second step is skippable and posts an empty list when skipped, so
    assigning interests unconditionally turned "fix my date of birth" into
    "lose everything you chose".
    """
    await _account(session, user_id)
    assert await _onboard(client, auth_headers, birth_date=_born(30), interests=["biznes"]) == 200
    assert await _onboard(client, auth_headers, birth_date=_born(31), interests=[]) == 200

    profile = await _profile(session, user_id)
    assert profile.interests == ["biznes"]
    assert age_from_profile(profile) == 31
