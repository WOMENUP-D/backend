"""The recommendation endpoints: hers only, built once, rebuilt on request,
and never naming a course that has since been unpublished."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.constants import Language, ProgramCategory, ProgramFormat, ScoreDimension
from app.models.assessment import Assessment
from app.models.program import Program
from app.models.user import User
from app.seed_skills import load_skills

URL = "/api/v1/diagnostic/recommendations"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _setup(session, user_id: uuid.UUID) -> Program:
    await load_skills(session)
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()
    program = Program(
        slug="rezyume-kurs",
        title_i18n={"ru": "Резюме за вечер"},
        goal_i18n={},
        description_i18n={"ru": "Как написать резюме."},
        category=ProgramCategory.VOCATIONAL_SKILLS,
        format=ProgramFormat.VIDEO,
        language=Language.RU,
        skills_taught=["резюме", "собеседование"],
        is_published=True,
        published_at=datetime.now(UTC),
        provider="Stepik",
        external_url="https://stepik.org/course/1",
    )
    session.add(program)
    session.add(
        Assessment(
            user_id=user_id,
            version=3,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            is_baseline=True,
            overall_score=50,
            dimension_scores={d.value: 60 for d in ScoreDimension},
            goals=["find_job"],
            profile={
                "version": 3,
                "situation": "graduate",
                "goal_primary": "first_job",
                "goals": ["first_job"],
                "goals_other": [],
                "interests": ["business"],
                "skill_levels": {},
                "difficulties": ["cv"],
                "hours": "h2_5",
                "language": "ru",
            },
        )
    )
    await session.commit()
    return program


@pytest.mark.asyncio
async def test_needs_an_account_and_an_attempt(app_client, client, auth_headers, session, user_id):
    assert (await app_client.get(URL)).status_code == 401
    session.add(User(id=user_id, phone=_phone()))
    await session.commit()
    assert (await client.get(URL, headers=auth_headers)).status_code == 404


@pytest.mark.asyncio
async def test_built_once_then_served_from_the_attempt(client, session, auth_headers, user_id):
    program = await _setup(session, user_id)
    first = await client.get(URL, headers=auth_headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["mode"] == "rules"  # the model is off under test
    assert body["courses"][0]["program"]["id"] == str(program.id)
    assert body["courses"][0]["priority"] == "high"
    assert body["courses"][0]["why_i18n"]["uz"] and body["courses"][0]["outcome_i18n"]["ru"]
    covers = {c["slug"]: c["name_i18n"] for c in body["courses"][0]["covers"]}
    assert set(covers) == {"cv-writing", "job-interviews"}
    assert covers["cv-writing"]["ru"] == "Составление резюме"
    assert body["roadmap"] and body["roadmap"][0]["items"][0]["program_id"] == str(program.id)
    assert any(step["activity"] == "cv" for step in body["roadmap"][-1]["items"])
    assert body["gaps"] and body["summary"]["gaps_i18n"]["ru"]

    second = await client.get(URL, headers=auth_headers)
    assert second.json()["generated_at"] == body["generated_at"]

    refreshed = await client.post(f"{URL}/refresh", headers=auth_headers)
    assert refreshed.status_code == 200
    assert refreshed.json()["generated_at"] >= body["generated_at"]


@pytest.mark.asyncio
async def test_an_unpublished_course_disappears_from_a_stored_answer(
    client, session, auth_headers, user_id
):
    program = await _setup(session, user_id)
    assert (await client.get(URL, headers=auth_headers)).json()["courses"]
    program.is_published = False
    await session.commit()
    body = (await client.get(URL, headers=auth_headers)).json()
    assert body["courses"] == []
    assert all(item["kind"] != "course" for stage in body["roadmap"] for item in stage["items"])


@pytest.mark.asyncio
async def test_someone_elses_attempt_is_not_found(client, session, auth_headers, user_id):
    other = uuid.uuid4()
    await _setup(session, other)
    session.add(User(id=user_id, phone=_phone()))
    await session.commit()
    attempt_id = (await session.execute(__import__("sqlalchemy").select(Assessment.id))).scalar()
    response = await client.get(URL, params={"attempt_id": str(attempt_id)}, headers=auth_headers)
    assert response.status_code == 404
