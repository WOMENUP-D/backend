"""The cabinet's recommendation endpoints: signed in only, and only about the caller."""

import uuid

import pytest

from app.core.constants import ScoreDimension
from app.models.assessment import AssessmentQuestion
from app.models.plan import DevelopmentPlan, PlanItem
from app.models.user import User

RECOMMENDATIONS = "/api/v1/ai/recommendations"
INSIGHTS = "/api/v1/assessments/score/insights"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


@pytest.mark.asyncio
async def test_both_need_an_account(app_client):
    for path in (RECOMMENDATIONS, INSIGHTS):
        assert (await app_client.get(path)).status_code == 401


@pytest.mark.asyncio
async def test_insights_wait_for_an_assessment(client, auth_headers):
    response = await client.get(INSIGHTS, headers=auth_headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_submitting_the_diagnostic_produces_insights(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    questions = [
        AssessmentQuestion(dimension=dimension, order_index=0, text_i18n={"en": dimension.value})
        for dimension in (ScoreDimension.EMPLOYMENT, ScoreDimension.HEALTHY_LIFESTYLE)
    ]
    session.add_all(questions)
    await session.flush()

    submitted = await client.post(
        "/api/v1/assessments/submit",
        json={
            "answers": [
                {"question_id": str(questions[0].id), "value": 25},
                {"question_id": str(questions[1].id), "value": 100},
            ]
        },
        headers=auth_headers,
    )
    assert submitted.status_code == 200

    response = await client.get(INSIGHTS, headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert {d["dimension"]: d["band"] for d in body["dimensions"]} == {
        "employment": "focus",
        "healthy_lifestyle": "strong",
    }
    assert body["focus_dimensions"] == ["employment"]


@pytest.mark.asyncio
async def test_recommendations_are_built_from_the_callers_records_only(
    client, session, auth_headers, user_id
):
    session.add(User(id=user_id, phone=_phone()))
    stranger = User(phone=_phone())
    session.add(stranger)
    await session.flush()
    session.add(
        DevelopmentPlan(
            user_id=stranger.id,
            title="Someone else's plan",
            is_active=True,
            items=[PlanItem(order_index=0, action="A step that is not hers")],
        )
    )
    await session.flush()

    response = await client.get(RECOMMENDATIONS, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["assessed"] is False
    assert body["next_steps"][0]["kind"] == "take_assessment"
    assert all(step["kind"] != "plan_item" for step in body["next_steps"])
