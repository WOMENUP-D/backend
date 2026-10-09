"""Plans for one month and for three: every step fits inside the horizon, a
month holds a month's worth, and the focus follows the latest check-in."""

import uuid
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import GoalHorizon, ScoreDimension
from app.models.assessment import Assessment
from app.models.user import User
from app.services.llm_gateway import LlmResponse
from app.services.plan_service import (
    HORIZON_DAYS,
    MAX_ITEMS,
    due_for,
    generate_plan,
    spread_due,
)

TODAY = date(2026, 10, 8)


class _Offline:
    enabled = False


class _Model:
    """A model that returns the steps it is given and keeps the prompt."""

    enabled = True

    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.prompt = ""

    async def complete(self, *, system, messages, json_schema=None, **_):
        self.prompt = messages[0]["content"]
        return LlmResponse(
            text="{}",
            trace_id="t",
            model="test",
            prompt_version="test",
            latency_ms=1,
            parsed={"title": "Reja", "summary": "", "items": self.items},
        )


def _step(week: int, dimension: str = "employment") -> dict:
    return {
        "action": f"Qadam {week}",
        "description": "",
        "dimension": dimension,
        "priority": "medium",
        "week_offset": week,
        "program_id": None,
        "rationale": "",
    }


def _user(session: AsyncSession) -> User:
    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}")
    session.add(user)
    return user


# --- dates ------------------------------------------------------------------


def test_a_step_is_due_at_the_end_of_its_week():
    assert due_for(GoalHorizon.M3, 0, TODAY) == TODAY + timedelta(days=7)
    assert due_for(GoalHorizon.M3, 4, TODAY) == TODAY + timedelta(days=35)


def test_no_step_falls_due_after_the_horizon():
    assert due_for(GoalHorizon.M1, 10, TODAY) == TODAY + timedelta(days=30)
    assert due_for(GoalHorizon.M3, 40, TODAY) == TODAY + timedelta(days=90)


def test_a_nonsense_offset_means_this_week():
    for value in (None, "soon", -3):
        assert due_for(GoalHorizon.M1, value, TODAY) == TODAY + timedelta(days=7)


def test_rule_based_steps_are_spread_across_the_horizon():
    dues = [spread_due(GoalHorizon.M1, i, 3, TODAY) for i in range(3)]
    assert dues == sorted(dues)
    assert dues[-1] == TODAY + timedelta(days=30)
    assert all(due > TODAY for due in dues)


# --- generation -------------------------------------------------------------


async def test_a_one_month_plan_holds_a_months_worth(session: AsyncSession):
    user = _user(session)
    await session.flush()
    model = _Model([_step(week) for week in range(10)])

    plan = await generate_plan(
        session, user_id=user.id, horizon=GoalHorizon.M1, language="uz", gateway=model
    )

    assert plan.horizon == GoalHorizon.M1
    assert len(plan.items) == MAX_ITEMS[GoalHorizon.M1]
    last = date.today() + timedelta(days=HORIZON_DAYS[GoalHorizon.M1])
    assert all(item.due_date <= last for item in plan.items)
    assert "HORIZON: 1 months (4 weeks; week_offset 0-3)" in model.prompt


async def test_a_three_month_plan_keeps_its_weeks(session: AsyncSession):
    user = _user(session)
    await session.flush()
    model = _Model([_step(0), _step(5), _step(11)])

    plan = await generate_plan(
        session, user_id=user.id, horizon=GoalHorizon.M3, language="uz", gateway=model
    )

    dues = sorted(item.due_date - date.today() for item in plan.items)
    assert dues == [timedelta(days=7), timedelta(days=42), timedelta(days=84)]


async def test_without_the_model_a_month_still_fits_in_a_month(session: AsyncSession):
    user = _user(session)
    await session.flush()

    plan = await generate_plan(
        session,
        user_id=user.id,
        horizon=GoalHorizon.M1,
        focus_dimensions=[ScoreDimension.EMPLOYMENT, ScoreDimension.DIGITAL_SKILLS],
        language="ru",
        gateway=_Offline(),  # type: ignore[arg-type]
    )

    assert plan.generated_by_ai is False
    assert all(item.due_date <= date.today() + timedelta(days=30) for item in plan.items)


async def test_the_plan_starts_from_the_check_in_priorities(session: AsyncSession):
    user = _user(session)
    await session.flush()
    session.add(
        Assessment(
            user_id=user.id,
            version=2,
            completed_at=date.today(),
            overall_score=50,
            dimension_scores={},
            goals=["business"],
            priorities=[
                {"dimension": "entrepreneurship", "priority": 70, "eligible": True},
                {"dimension": "family_parenting", "priority": 65, "eligible": False},
                {"dimension": "digital_skills", "priority": 60, "eligible": True},
            ],
        )
    )
    await session.flush()
    model = _Model([_step(0, "entrepreneurship")])

    plan = await generate_plan(
        session, user_id=user.id, horizon=GoalHorizon.M3, language="uz", gateway=model
    )

    assert plan.rationale["focus_dimensions"] == ["entrepreneurship", "digital_skills"]
    assert plan.rationale["from_diagnostic"] is True
    assert "DIAGNOSTIC PRIORITIES (highest first): entrepreneurship, digital_skills" in model.prompt
    assert "GOALS CHOSEN IN THE DIAGNOSTIC: business" in model.prompt


async def test_the_api_accepts_a_one_month_horizon(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=f"+9989{uuid.uuid4().int % 10**8:08d}"))
    await session.flush()
    response = await client.post(
        "/api/v1/plans/generate", json={"horizon": "1m", "language": "uz"}, headers=auth_headers
    )
    assert response.status_code in (200, 201)
    assert response.json()["horizon"] == "1m"
