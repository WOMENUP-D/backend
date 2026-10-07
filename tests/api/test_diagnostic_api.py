"""Diagnostic v2 over HTTP: questions, attempts, results, history, and the
score moving afterwards only for real, deduplicated, capped events."""

import uuid

import pytest
from sqlalchemy import func, select

from app.core.constants import ProgramCategory, ScoreDimension
from app.core.security import create_token
from app.models.assessment import Assessment, AssessmentQuestion, DevelopmentScore
from app.models.program import Program
from app.models.user import User
from app.services import diagnostic, score_progress

BASE = "/api/v1/diagnostic"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _instrument(session) -> list[AssessmentQuestion]:
    questions = [
        AssessmentQuestion(**row) for row in diagnostic.question_rows(diagnostic.load_bank())
    ]
    session.add_all(questions)
    await session.flush()
    return questions


def _payload(questions, letter="c", goals=("find_job",), **extra) -> dict:
    answers = []
    for q in questions:
        if q.question_type == "goals":
            ids = list(goals)
        elif q.question_type == "routing":
            ids = ["a"]
        else:
            ids = [letter]
        answers.append({"question_id": str(q.id), "option_ids": ids})
    return {"answers": answers, **extra}


def _headers_for(user_id: uuid.UUID) -> dict:
    from app.core.constants import Role

    token = create_token(user_id, Role.USER, "access", {"roles": [Role.USER.value]})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_every_route_needs_an_account(app_client):
    for method, path in (
        ("get", "/questions"),
        ("post", "/attempt"),
        ("get", "/result"),
        ("get", "/history"),
    ):
        response = await getattr(app_client, method)(BASE + path)
        assert response.status_code == 401, path


@pytest.mark.asyncio
async def test_questions_come_in_order_without_their_scores(client, session, auth_headers):
    await _instrument(session)
    response = await client.get(f"{BASE}/questions", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 2
    codes = [q["code"] for q in body["questions"]]
    assert codes[0] == "edu.q1" and codes[-1] == "goals.q25" and len(codes) == 25
    for question in body["questions"]:
        for option in question["options"]:
            assert set(option) == {"id", "label_i18n"}
    goals = body["questions"][-1]
    assert goals["type"] == "goals" and goals["max_choices"] == 3 and goals["dimension"] is None


@pytest.mark.asyncio
async def test_an_attempt_is_scored_on_the_server(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    questions = await _instrument(session)

    # Whatever score the browser claims is ignored: only the options count.
    payload = _payload(questions, "a", overall=100, score=100)
    for answer in payload["answers"]:
        answer["value"] = 100
    response = await client.post(f"{BASE}/attempt", json=payload, headers=auth_headers)
    assert response.status_code == 201
    body = response.json()
    assert body["overall"] == 0
    assert body["level"] == "starting_point"
    assert {d["dimension"]: d["score"] for d in body["dimensions"]} == {
        d.value: 0 for d in ScoreDimension
    }
    assert body["is_baseline"] is True
    assert len(body["growth"]) == 3
    assert len(body["strongest"]) == 2
    assert not set(body["strongest"]) & {g["dimension"] for g in body["growth"]}
    assert 1 <= len(body["next_steps"]) <= 3
    assert body["goals"] == ["find_job"]
    # Career is her goal and her lowest point together: it leads.
    assert body["growth"][0]["dimension"] == "employment"


@pytest.mark.asyncio
async def test_an_invalid_attempt_stores_nothing(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    questions = await _instrument(session)

    incomplete = _payload(questions)
    incomplete["answers"] = incomplete["answers"][:-3]
    response = await client.post(f"{BASE}/attempt", json=incomplete, headers=auth_headers)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "incomplete"

    bad = _payload(questions)
    bad["answers"][0]["option_ids"] = ["x"]
    response = await client.post(f"{BASE}/attempt", json=bad, headers=auth_headers)
    assert response.json()["detail"]["code"] == "invalid_answer"

    unknown = _payload(questions)
    unknown["answers"][0]["question_id"] = str(uuid.uuid4())
    response = await client.post(f"{BASE}/attempt", json=unknown, headers=auth_headers)
    assert response.json()["detail"]["code"] == "invalid_question"

    stored = await session.scalar(select(func.count()).select_from(Assessment))
    assert stored == 0


@pytest.mark.asyncio
async def test_the_same_attempt_sent_twice_is_stored_once(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    questions = await _instrument(session)
    payload = _payload(questions, client_ref=str(uuid.uuid4()))

    first = await client.post(f"{BASE}/attempt", json=payload, headers=auth_headers)
    second = await client.post(f"{BASE}/attempt", json=payload, headers=auth_headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["attempt_id"] == second.json()["attempt_id"]
    assert await session.scalar(select(func.count()).select_from(Assessment)) == 1


@pytest.mark.asyncio
async def test_a_new_attempt_keeps_the_old_one(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    questions = await _instrument(session)

    first = (
        await client.post(f"{BASE}/attempt", json=_payload(questions, "b"), headers=auth_headers)
    ).json()
    second = (
        await client.post(f"{BASE}/attempt", json=_payload(questions, "d"), headers=auth_headers)
    ).json()
    assert second["is_baseline"] is False

    history = (await client.get(f"{BASE}/history", headers=auth_headers)).json()
    assert [h["attempt_id"] for h in history] == [second["attempt_id"], first["attempt_id"]]
    assert [h["overall"] for h in history] == [75, 25]

    latest = (await client.get(f"{BASE}/result", headers=auth_headers)).json()
    assert latest["attempt_id"] == second["attempt_id"]
    earlier = await client.get(
        f"{BASE}/result", params={"attempt_id": first["attempt_id"]}, headers=auth_headers
    )
    assert earlier.json()["overall"] == 25


@pytest.mark.asyncio
async def test_nobody_reads_another_womans_result(client, session, auth_headers, user_id):
    stranger = User(phone=_phone())
    session.add_all([User(id=user_id, phone=_phone()), stranger])
    questions = await _instrument(session)
    theirs = (
        await client.post(
            f"{BASE}/attempt", json=_payload(questions), headers=_headers_for(stranger.id)
        )
    ).json()

    assert (await client.get(f"{BASE}/result", headers=auth_headers)).status_code == 404
    response = await client.get(
        f"{BASE}/result", params={"attempt_id": theirs["attempt_id"]}, headers=auth_headers
    )
    assert response.status_code == 404
    assert (await client.get(f"{BASE}/history", headers=auth_headers)).json() == []


@pytest.mark.asyncio
async def test_the_old_submit_that_trusted_the_browser_is_gone(client, auth_headers):
    response = await client.post(
        "/api/v1/assessments/submit",
        json={"answers": [{"question_id": str(uuid.uuid4()), "value": 100}]},
        headers=auth_headers,
    )
    assert response.status_code == 410


# --- the score after the diagnostic ------------------------------------------------


async def _measured(client, session, user_id, auth_headers, letter="c"):
    session.add(User(id=user_id, phone=_phone()))
    questions = await _instrument(session)
    await client.post(f"{BASE}/attempt", json=_payload(questions, letter), headers=auth_headers)


async def _current(session, user_id, dimension) -> float:
    return await session.scalar(
        select(DevelopmentScore.current).where(
            DevelopmentScore.user_id == user_id, DevelopmentScore.dimension == dimension
        )
    )


@pytest.mark.asyncio
async def test_a_finished_course_moves_its_dimension_once(client, session, auth_headers, user_id):
    await _measured(client, session, user_id, auth_headers)
    program = Program(
        slug="ai-basics",
        title_i18n={"en": "AI basics"},
        category=ProgramCategory.DIGITAL_SAFETY,
        is_published=True,
    )
    session.add(program)
    await session.flush()

    assert await score_progress.on_program_completed(session, user_id=user_id, program=program)
    assert await _current(session, user_id, ScoreDimension.DIGITAL_SKILLS) == 54.0
    # The same course again earns nothing.
    assert not await score_progress.on_program_completed(session, user_id=user_id, program=program)
    assert await _current(session, user_id, ScoreDimension.DIGITAL_SKILLS) == 54.0


@pytest.mark.asyncio
async def test_adjustments_are_capped_and_stay_within_100(client, session, auth_headers, user_id):
    await _measured(client, session, user_id, auth_headers, "d")  # 75 everywhere
    for _ in range(12):  # 12 × 3 = 36 points claimed
        await score_progress.on_application_submitted(
            session, user_id=user_id, opportunity_id=uuid.uuid4()
        )
    assert await _current(session, user_id, ScoreDimension.EMPLOYMENT) == 95.0  # 75 + cap 20
    assert score_progress.adjusted(95.0, 20.0) == 100.0


@pytest.mark.asyncio
async def test_a_cv_counts_once_per_measurement(client, session, auth_headers, user_id):
    await _measured(client, session, user_id, auth_headers)
    assert await score_progress.on_cv_updated(session, user_id=user_id)
    assert not await score_progress.on_cv_updated(session, user_id=user_id)
    assert await _current(session, user_id, ScoreDimension.EMPLOYMENT) == 54.0


@pytest.mark.asyncio
async def test_nothing_is_awarded_before_a_diagnostic(session, user_id):
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()
    assert not await score_progress.on_cv_updated(session, user_id=user_id)


@pytest.mark.asyncio
async def test_a_new_diagnostic_starts_from_its_own_measurement(
    client, session, auth_headers, user_id
):
    await _measured(client, session, user_id, auth_headers)
    await score_progress.on_cv_updated(session, user_id=user_id)
    assert await _current(session, user_id, ScoreDimension.EMPLOYMENT) == 54.0

    questions = list(
        (
            await session.execute(
                select(AssessmentQuestion).order_by(AssessmentQuestion.order_index)
            )
        ).scalars()
    )
    await client.post(f"{BASE}/attempt", json=_payload(questions, "c"), headers=auth_headers)
    assert await _current(session, user_id, ScoreDimension.EMPLOYMENT) == 50.0
    # …and the CV can count again on top of the new reading.
    assert await score_progress.on_cv_updated(session, user_id=user_id)
