"""Plan fixes from the QA audit: D-02, D-09, D-16, D-17."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.core.constants import GoalHorizon, ProgramCategory, ScoreDimension
from app.models.plan import DevelopmentPlan, PlanItem
from app.models.program import Program
from app.models.user import User
from app.services.plan_service import generate_plan


class _Offline:
    enabled = False


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def _plan(session, user_id, *, active: bool, statuses=("planned", "planned")):
    plan = DevelopmentPlan(
        user_id=user_id,
        horizon=GoalHorizon.M3,
        title="Reja",
        is_active=active,
        rationale={},
        accepted_at=None,
    )
    session.add(plan)
    await session.flush()
    for i, s in enumerate(statuses):
        session.add(PlanItem(plan_id=plan.id, order_index=i, action=f"Qadam {i}", status=s))
    await session.flush()
    await session.refresh(plan, ["items"])
    return plan


async def test_a_skipped_step_does_not_hold_the_plan_below_100(session):
    user = User(phone=_phone())
    session.add(user)
    await session.flush()
    plan = await _plan(session, user.id, active=True, statuses=("done", "skipped"))
    assert plan.progress_percent == 100


async def test_a_draft_is_read_only(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()
    plan = await _plan(session, user_id, active=False)
    response = await client.patch(
        f"/api/v1/plans/items/{plan.items[0].id}", json={"status": "done"}, headers=auth_headers
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "plan_not_active"


async def test_due_dates_stay_inside_the_horizon(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()
    plan = await _plan(session, user_id, active=True)
    item = plan.items[0]
    for bad in ("1999-01-01", "2099-01-01"):
        response = await client.patch(
            f"/api/v1/plans/items/{item.id}", json={"due_date": bad}, headers=auth_headers
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "due_date_out_of_range"
    fine = (date.today() + timedelta(days=10)).isoformat()
    response = await client.patch(
        f"/api/v1/plans/items/{item.id}", json={"due_date": fine}, headers=auth_headers
    )
    assert response.status_code == 200


async def test_a_step_without_a_course_is_marked_by_her_and_can_be_unmarked(
    client, session, auth_headers, user_id
):
    """D-02: steps with no course of ours can only close by her own mark."""
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()
    plan = await _plan(session, user_id, active=True)
    item = plan.items[0]
    done = await client.patch(
        f"/api/v1/plans/items/{item.id}", json={"status": "done"}, headers=auth_headers
    )
    assert done.status_code == 200
    assert done.json()["self_report"] is True
    assert done.json()["completed_at"] is not None
    undone = await client.patch(
        f"/api/v1/plans/items/{item.id}", json={"status": "planned"}, headers=auth_headers
    )
    assert undone.json()["completed_at"] is None


async def test_a_new_draft_replaces_the_old_unaccepted_one(session):
    """D-17: "another option" used to leave every earlier draft behind."""
    user = User(phone=_phone())
    session.add(user)
    await session.flush()
    accepted = await _plan(session, user.id, active=True)
    first = await generate_plan(
        session, user_id=user.id, horizon=GoalHorizon.M1, gateway=_Offline()
    )
    second = await generate_plan(
        session, user_id=user.id, horizon=GoalHorizon.M3, gateway=_Offline()
    )
    ids = set((await session.execute(select(DevelopmentPlan.id))).scalars())
    assert accepted.id in ids and second.id in ids and first.id not in ids


async def test_the_fallback_plan_names_areas_in_words_and_grows_with_the_horizon(session):
    """D-09: no raw enum values, the reformed alphabet, more steps for longer horizons."""
    user = User(phone=_phone())
    session.add(user)
    for i in range(4):
        session.add(
            Program(
                slug=f"fin-{i}",
                title_i18n={"uz": f"Moliya {i}"},
                category=ProgramCategory.FINANCIAL_LITERACY,
                is_published=True,
            )
        )
    await session.flush()
    focus = [ScoreDimension.FINANCIAL_LITERACY, ScoreDimension.LEADERSHIP]
    month = await generate_plan(
        session,
        user_id=user.id,
        horizon=GoalHorizon.M1,
        focus_dimensions=focus,
        language="uz",
        gateway=_Offline(),
    )
    month_count = len(month.items)
    quarter = await generate_plan(
        session,
        user_id=user.id,
        horizon=GoalHorizon.M3,
        focus_dimensions=focus,
        language="ru",
        gateway=_Offline(),
    )
    await session.refresh(quarter, ["items"])
    assert len(quarter.items) > month_count
    actions = " ".join(item.action for item in quarter.items)
    assert "leadership" not in actions and "financial_literacy" not in actions
    assert month.title == "Boşlanğiç rivojlaniş rejasi"
