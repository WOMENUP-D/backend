"""Saving a profile returns the saved profile.

`updated_at` is filled by the database, not by the application, so an UPDATE
leaves the attribute expired on the instance. Serialising the response then
reaches for it outside the async context and the whole request dies with
MissingGreenlet — which is what happened: every attempt to fill in a profile
came back 500, and nobody could complete one at all.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


@pytest_asyncio.fixture
async def signed_in(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    """The token fixture mints an id; the audit row this endpoint writes has a
    foreign key to `users`, so the account behind that id has to exist."""
    session.add(User(id=user_id, phone=f"+9989{user_id.int % 10**8:08d}"))
    await session.flush()
    return user_id


@pytest.mark.asyncio
async def test_a_profile_can_be_saved_and_comes_back(
    client: AsyncClient, auth_headers: dict[str, str], signed_in: uuid.UUID
) -> None:
    body = {
        "full_name": "Dilnoza Karimova",
        "profession": "Buxgalter",
        "education_level": "Oliy",
        "years_of_experience": 5,
        "district": "Toshkent",
        "skills": ["Excel", "1C"],
        "languages": ["uz", "ru"],
        "bio": "Moliyaviy tahlilga oʻtmoqchiman.",
    }
    response = await client.put("/api/v1/users/me/profile", json=body, headers=auth_headers)

    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["full_name"] == "Dilnoza Karimova"
    assert saved["skills"] == ["Excel", "1C"]
    # The field that used to blow the response up.
    assert saved["updated_at"]
    assert saved["completeness_percent"] > 0


@pytest.mark.asyncio
async def test_saving_twice_still_works(
    client: AsyncClient, auth_headers: dict[str, str], signed_in: uuid.UUID
) -> None:
    """The second write is the one that takes the UPDATE path rather than the
    INSERT path, and the UPDATE path is where the bug lived."""
    first = await client.put(
        "/api/v1/users/me/profile", json={"profession": "Buxgalter"}, headers=auth_headers
    )
    assert first.status_code == 200, first.text

    second = await client.put(
        "/api/v1/users/me/profile", json={"profession": "Moliyaviy tahlilchi"}, headers=auth_headers
    )
    assert second.status_code == 200, second.text
    assert second.json()["profession"] == "Moliyaviy tahlilchi"
