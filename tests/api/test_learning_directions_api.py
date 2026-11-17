"""`GET /learning-paths/for-me` — the directions her diagnostic points to."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.constants import Language, ProficiencyLevel, ProgramCategory
from app.models.assessment import Assessment
from app.models.learning_path import UserLearningPath
from app.models.program import Enrollment, Program
from app.models.user import User

URL = "/api/v1/learning-paths/for-me"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _her(session, user_id) -> None:
    session.add(User(id=user_id, phone=_phone(), language=Language.UZ))
    await session.flush()


async def _attempt(session, user_id, *, interests=None, scores=None, when=None) -> Assessment:
    attempt = Assessment(
        user_id=user_id,
        version=4,
        started_at=when or datetime.now(UTC),
        completed_at=when or datetime.now(UTC),
        overall_score=50.0,
        dimension_scores=scores or {},
        profile={"version": 4, "interests": interests} if interests is not None else None,
    )
    session.add(attempt)
    await session.flush()
    return attempt


async def _course(
    session,
    title: str,
    tags: list[str],
    *,
    level: ProficiencyLevel | None = None,
    category: ProgramCategory = ProgramCategory.DIGITAL_SKILLS,
    published: bool = True,
) -> Program:
    program = Program(
        slug=f"c-{uuid.uuid4().hex[:10]}",
        title_i18n={"ru": title},
        description_i18n={},
        category=category,
        language=Language.RU,
        level=level,
        skills_taught=tags,
        external_url="https://stepik.org/course/1",
        source="stepik",
        is_published=published,
        published_at=datetime.now(UTC) if published else None,
    )
    session.add(program)
    await session.flush()
    return program


async def test_signed_in_only(client):
    assert (await client.get(URL)).status_code == 401


async def test_no_diagnostic_asks_for_one(client, session, auth_headers, user_id):
    await _her(session, user_id)
    # An unfinished attempt is not a diagnostic.
    session.add(Assessment(user_id=user_id, started_at=datetime.now(UTC)))
    await session.flush()

    response = await client.get(URL, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["needs_diagnostic"] is True
    assert body["directions"] == [] and body["basis"] is None


async def test_it_interests_bring_it_directions_with_ordered_courses(
    client, session, auth_headers, user_id
):
    await _her(session, user_id)
    await _attempt(session, user_id, interests=["it_programming"])
    advanced = await _course(session, "Python: продвинутый уровень", ["python"])
    later = await _course(session, "Python", ["python"], level=ProficiencyLevel.ADVANCED)
    first = await _course(session, "Python", ["python"], level=ProficiencyLevel.BEGINNER)
    for n in range(7):
        await _course(session, f"Python {n}", ["python"])
    await _course(session, "Python (скрыт)", ["python"], published=False)
    # Two courses are not a direction.
    await _course(session, "Кибербезопасность", ["кибербезопасность"])
    await _course(session, "Кибербезопасность 2", ["кибербезопасность"])
    # A field she did not choose stays out.
    for n in range(3):
        await _course(session, f"Английский {n}", ["английский язык"])

    response = await client.get(URL, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["needs_diagnostic"] is False and body["basis"] == "interests"
    assert [i["id"] for i in body["interests"]] == ["it_programming"]
    assert body["interests"][0]["label_i18n"]["uz"] == "IT va dasturlaş"
    slugs = [d["slug"] for d in body["directions"]]
    assert "python" in slugs
    assert "cybersecurity" not in slugs and "english" not in slugs

    python = next(d for d in body["directions"] if d["slug"] == "python")
    assert python["reason"] == "interest" and python["interest"]["id"] == "it_programming"
    assert python["course_total"] == 10
    ids = [c["id"] for c in python["courses"]]
    assert len(ids) == 8 and len(set(ids)) == 8
    assert ids[0] == str(first.id)
    assert str(later.id) not in ids and str(advanced.id) not in ids  # last two, past the cap
    assert all(c["is_published"] for c in python["courses"])


async def test_latest_attempt_decides(client, session, auth_headers, user_id):
    await _her(session, user_id)
    await _attempt(
        session, user_id, interests=["it_programming"], when=datetime.now(UTC) - timedelta(days=9)
    )
    await _attempt(session, user_id, interests=["languages"])
    for n in range(3):
        await _course(session, f"Python {n}", ["python"])
        await _course(session, f"Английский {n}", ["английский язык"])

    body = (await client.get(URL, headers=auth_headers)).json()

    assert [d["slug"] for d in body["directions"]] == ["english"]


async def test_other_falls_back_to_her_weakest_area_and_says_so(
    client, session, auth_headers, user_id
):
    await _her(session, user_id)
    await _attempt(
        session,
        user_id,
        interests=["other"],
        scores={"financial_literacy": 10, "digital_skills": 90, "employment": 80},
    )
    for n in range(4):
        await _course(session, f"Финансы {n}", ["финансовая грамотность"])
        await _course(session, f"Python {n}", ["python"])

    body = (await client.get(URL, headers=auth_headers)).json()

    assert body["basis"] == "weakest_dimension"
    assert body["unmatched_interests"] == []
    first = body["directions"][0]
    assert first["slug"] == "financial-literacy"
    assert first["reason"] == "weakest_dimension" and first["dimension"] == "financial_literacy"


async def test_with_no_match_anywhere_the_best_stocked_are_offered(
    client, session, auth_headers, user_id
):
    await _her(session, user_id)
    await _attempt(session, user_id, interests=["design"])  # no design courses
    for n in range(5):
        await _course(session, f"Английский {n}", ["английский язык"])

    body = (await client.get(URL, headers=auth_headers)).json()

    assert body["basis"] == "popular"
    assert [i["id"] for i in body["unmatched_interests"]] == ["design"]
    assert [d["slug"] for d in body["directions"]] == ["english"]
    assert body["directions"][0]["reason"] == "popular"


async def test_reading_writes_nothing(client, session, auth_headers, user_id):
    await _her(session, user_id)
    await _attempt(session, user_id, interests=["it_programming"])
    for n in range(3):
        await _course(session, f"Python {n}", ["python"])

    assert (await client.get(URL, headers=auth_headers)).status_code == 200

    for model in (Enrollment, UserLearningPath):
        count = await session.scalar(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        )
        assert count == 0


async def test_for_me_is_not_read_as_a_path_slug(client, session, auth_headers, user_id):
    await _her(session, user_id)
    response = await client.get(URL, headers=auth_headers)
    assert response.status_code == 200 and "needs_diagnostic" in response.json()
