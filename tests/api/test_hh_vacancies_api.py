"""An hh.uz vacancy on the API: shown with its source and its link, applied to
on hh.uz and nowhere else, with its courses open to anyone."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.constants import (
    Language,
    OpportunitySource,
    OpportunityType,
    ProgramCategory,
    ProgramFormat,
    Region,
    Role,
)
from app.core.security import create_token
from app.models.opportunity import Application, Opportunity
from app.models.program import Program
from app.models.user import User

BASE = "/api/v1/opportunities"


def _headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_token(user_id, Role.USER, "access", {"roles": [Role.USER.value]})
    return {"Authorization": f"Bearer {token}"}


async def _hh_vacancy(session) -> Opportunity:
    vacancy = Opportunity(
        source=OpportunitySource.HH_UZ,
        external_id="777",
        type=OpportunityType.VACANCY,
        title_i18n={"ru": "Бухгалтер"},
        description_i18n={
            "ru": "От 1 года до 3 лет · Полная занятость\n\nВедение учёта в 1С и Excel."
        },
        organisation="Alfa Savdo",
        region=Region.TASHKENT_CITY.value,
        required_skills=["Excel", "1С: Бухгалтерия"],
        eligibility={},
        reward={"salary_from": 6000000, "currency": "UZS"},
        external_url="https://hh.uz/vacancy/777",
        is_active=True,
        synced_at=datetime.now(UTC),
    )
    session.add(vacancy)
    session.add(
        Program(
            slug="excel-functions",
            title_i18n={"ru": "Полезные функции Excel"},
            goal_i18n={},
            description_i18n={},
            category=ProgramCategory.VOCATIONAL_SKILLS,
            format=ProgramFormat.VIDEO,
            language=Language.RU,
            skills_taught=["excel"],
            is_published=True,
            published_at=datetime.now(UTC),
            provider="Stepik",
            external_url="https://stepik.org/course/1",
        )
    )
    await session.flush()
    return vacancy


async def _woman(session) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(
        User(
            id=user_id,
            phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
            email=f"{uuid.uuid4().hex[:8]}@example.uz",
            language=Language.UZ,
            region=Region.TASHKENT_CITY,
        )
    )
    await session.flush()
    return user_id


@pytest.mark.asyncio
async def test_the_listing_names_its_source_and_where_to_apply(client, session):
    vacancy = await _hh_vacancy(session)

    page = (await client.get(f"{BASE}/discover")).json()
    assert page["total"] == 1
    card = page["items"][0]
    assert card["source"] == "hh_uz"
    assert card["apply_url"] == "https://hh.uz/vacancy/777"
    assert card["reward"]["salary_from"] == 6000000

    detail = (await client.get(f"{BASE}/{vacancy.id}")).json()
    assert detail["apply_url"] == "https://hh.uz/vacancy/777"
    # No partner system stands behind an hh.uz listing: nothing is shared,
    # so there is no consent to ask for.
    assert detail["sharing"]["partner"] is None
    assert detail["sharing"]["fields"] == []
    assert detail["sharing"]["consent_given"] is False


@pytest.mark.asyncio
async def test_applying_on_the_platform_is_refused_because_she_applies_on_hh(client, session):
    vacancy = await _hh_vacancy(session)
    user_id = await _woman(session)

    response = await client.post(
        f"{BASE}/{vacancy.id}/apply", json={"consent": True}, headers=_headers(user_id)
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "apply_externally"
    assert response.json()["detail"]["url"] == "https://hh.uz/vacancy/777"

    from sqlalchemy import func, select

    assert (await session.scalar(select(func.count()).select_from(Application))) == 0


@pytest.mark.asyncio
async def test_the_courses_for_a_vacancy_are_open_to_visitors(client, session):
    vacancy = await _hh_vacancy(session)
    response = await client.get(f"{BASE}/{vacancy.id}/courses")
    assert response.status_code == 200
    body = response.json()
    assert body["opportunity_id"] == str(vacancy.id)
    # The model is off in tests, so the rules answer — and say so.
    assert body["mode"] == "rules"
    assert [item["program"]["slug"] for item in body["items"]] == ["excel-functions"]
    assert body["items"][0]["covers"] == ["Excel"]
    assert body["items"][0]["program"]["external_url"] == "https://stepik.org/course/1"

    # A second read serves the kept choice.
    again = (await client.get(f"{BASE}/{vacancy.id}/courses")).json()
    assert again["generated_at"] == body["generated_at"]


@pytest.mark.asyncio
async def test_courses_for_an_unknown_listing_are_a_404(client):
    response = await client.get(f"{BASE}/{uuid.uuid4()}/courses")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_catalogue_counts_the_feed_by_its_source(client, session):
    await _hh_vacancy(session)
    stats = (await client.get(f"{BASE}/stats")).json()
    assert stats["by_source"] == {"hh_uz": 1}
    assert stats["by_type"] == {"vacancy": 1}
