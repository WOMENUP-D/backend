"""Individual development plans."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUserDep, DbSession, TrackerDep
from app.models.plan import DevelopmentPlan, PlanItem
from app.schemas.common import Message
from app.schemas.plan import (
    PlanAccept,
    PlanGenerateRequest,
    PlanItemRead,
    PlanItemUpdate,
    PlanRead,
)
from app.services import telemetry
from app.services.plan_service import accept_plan, generate_plan

router = APIRouter(prefix="/plans", tags=["plans"])


@router.post("/generate", response_model=PlanRead, status_code=status.HTTP_201_CREATED)
async def generate(
    payload: PlanGenerateRequest, user: CurrentUserDep, session: DbSession, actor: TrackerDep
) -> DevelopmentPlan:
    """Draft a roadmap. It stays inactive until the user accepts it."""
    plan = await generate_plan(
        session,
        user_id=uuid.UUID(user.id),
        horizon=payload.horizon,
        focus_dimensions=payload.focus_dimensions or None,
        language=payload.language,
    )
    await telemetry.record_event(
        session,
        actor=actor,
        **telemetry.business("plan_generated", key=plan.id, entity_type="plan", entity_id=plan.id),
    )
    return plan


@router.get("/active", response_model=PlanRead)
async def read_active(user: CurrentUserDep, session: DbSession) -> DevelopmentPlan:
    plan = await session.scalar(
        select(DevelopmentPlan).where(
            DevelopmentPlan.user_id == uuid.UUID(user.id),
            DevelopmentPlan.is_active.is_(True),
        )
    )
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active plan")
    return plan


@router.get("", response_model=list[PlanRead])
async def list_plans(user: CurrentUserDep, session: DbSession) -> list[DevelopmentPlan]:
    rows = await session.execute(
        select(DevelopmentPlan)
        .where(DevelopmentPlan.user_id == uuid.UUID(user.id))
        .order_by(DevelopmentPlan.created_at.desc())
    )
    return list(rows.scalars())


@router.post("/{plan_id}/accept", response_model=PlanRead)
async def accept(
    plan_id: uuid.UUID,
    payload: PlanAccept,
    user: CurrentUserDep,
    session: DbSession,
    actor: TrackerDep,
) -> DevelopmentPlan:
    """Confirm a plan — the step that turns an AI proposal into the user's plan."""
    plan = await accept_plan(
        session,
        user_id=uuid.UUID(user.id),
        plan_id=plan_id,
        removed_item_ids=payload.removed_item_ids,
    )
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    await telemetry.record_event(
        session,
        actor=actor,
        **telemetry.business("plan_accepted", key=plan.id, entity_type="plan", entity_id=plan.id),
    )
    return plan


@router.patch("/items/{item_id}", response_model=PlanItemRead)
async def update_item(
    item_id: uuid.UUID,
    payload: PlanItemUpdate,
    user: CurrentUserDep,
    session: DbSession,
) -> PlanItem:
    item = await session.get(PlanItem, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    plan = await session.get(DevelopmentPlan, item.plan_id)
    if plan is None or plan.user_id != uuid.UUID(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    from datetime import UTC, datetime

    from app.core.constants import PlanItemStatus

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    if item.status == PlanItemStatus.DONE and item.completed_at is None:
        item.completed_at = datetime.now(UTC)

    await session.flush()
    return item


@router.delete("/{plan_id}", response_model=Message)
async def delete_plan(plan_id: uuid.UUID, user: CurrentUserDep, session: DbSession) -> Message:
    plan = await session.get(DevelopmentPlan, plan_id)
    if plan is None or plan.user_id != uuid.UUID(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    await session.delete(plan)
    return Message(detail="Plan deleted")
