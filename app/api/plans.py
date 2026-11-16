"""Individual development plans."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select

from app.api.deps import CurrentUserDep, DbSession, TrackerDep
from app.core.constants import PlanItemStatus, normalise_language
from app.models.plan import DevelopmentPlan, PlanItem
from app.schemas.common import Message
from app.schemas.plan import (
    PlanAccept,
    PlanGenerateRequest,
    PlanItemRead,
    PlanItemUpdate,
    PlanRead,
    PlanTranslateRequest,
)
from app.services import ai_limits, plan_service, plan_translation, telemetry
from app.services.plan_service import accept_plan, generate_plan

router = APIRouter(prefix="/plans", tags=["plans"])


@router.post("/generate", response_model=PlanRead, status_code=status.HTTP_201_CREATED)
async def generate(
    payload: PlanGenerateRequest,
    user: CurrentUserDep,
    session: DbSession,
    actor: TrackerDep,
    request: Request,
) -> DevelopmentPlan:
    """Draft a roadmap. It stays inactive until the user accepts it.

    Each draft is a long model call, so it spends the same hourly AI quota as
    the assistant (I-05) — taken first, because the counter commits.
    """
    # No diagnostic, no plan — checked before the quota, so the refusal is free.
    try:
        await plan_service.require_diagnostic(session, uuid.UUID(user.id))
    except plan_service.DiagnosticRequiredError as err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="diagnostic_required"
        ) from err
    await ai_limits.take(session, request, uuid.UUID(user.id))
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


#: `?language=` on the reads: the locale she is reading the site in. A cached
#: translation of the plan into it is applied; nothing is ever translated or
#: written on a read (`POST /plans/{id}/translate` does that).
LanguageQuery = Query(default=None, max_length=8, description="uz | uz-Cyrl | ru | en")


@router.get("/active", response_model=PlanRead)
async def read_active(
    user: CurrentUserDep, session: DbSession, language: str | None = LanguageQuery
) -> PlanRead:
    plan = await session.scalar(
        select(DevelopmentPlan).where(
            DevelopmentPlan.user_id == uuid.UUID(user.id),
            DevelopmentPlan.is_active.is_(True),
        )
    )
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active plan")
    # Read-only: a "sit the diagnostic" step is settled when the diagnostic
    # is submitted and when a plan is accepted (`plan_service.accept_plan`).
    return plan_translation.localise(plan, normalise_language(language))


@router.get("", response_model=list[PlanRead])
async def list_plans(
    user: CurrentUserDep, session: DbSession, language: str | None = LanguageQuery
) -> list[PlanRead]:
    rows = await session.execute(
        select(DevelopmentPlan)
        .where(DevelopmentPlan.user_id == uuid.UUID(user.id))
        .order_by(DevelopmentPlan.created_at.desc())
    )
    lang = normalise_language(language)
    return [plan_translation.localise(plan, lang) for plan in rows.scalars()]


@router.post("/{plan_id}/translate", response_model=PlanRead)
async def translate(
    plan_id: uuid.UUID,
    payload: PlanTranslateRequest,
    user: CurrentUserDep,
    session: DbSession,
    request: Request,
) -> PlanRead:
    """Her plan in the language she is now reading the site in.

    The plan is translated, never regenerated: her steps, dates and progress
    stay as they are. Translated once per language and cached; a plan already
    in that language, or already translated, costs nothing. A new translation
    spends one answer of the hourly AI quota; when the quota is spent or the
    model is unavailable the original comes back with
    `translation_unavailable` instead of an error.
    """
    plan = await session.get(DevelopmentPlan, plan_id)
    if plan is None or plan.user_id != uuid.UUID(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    language = normalise_language(payload.language)
    if language is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unsupported_language"
        )
    if language == plan.content_language or language in (plan.translations or {}):
        return plan_translation.localise(plan, language)
    try:
        await ai_limits.take(session, request, uuid.UUID(user.id))
    except HTTPException as exc:
        if exc.status_code != status.HTTP_429_TOO_MANY_REQUESTS:
            raise
        return plan_translation.localise(plan, language, unavailable=True)
    return await plan_translation.translate(session, plan, language, user_id=uuid.UUID(user.id))


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

    # A draft is read-only until she accepts it: ticks on a draft carried into
    # the accepted plan as progress she never made (QA audit D-16).
    if not plan.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="plan_not_active")

    changes = payload.model_dump(exclude_unset=True)
    if "due_date" in changes and changes["due_date"] is not None:
        start = (plan.accepted_at or plan.created_at).date()
        last = start + timedelta(days=plan_service.HORIZON_DAYS[plan.horizon])
        if not date.today() <= changes["due_date"] <= last:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="due_date_out_of_range"
            )

    for field, value in changes.items():
        setattr(item, field, value)
    if item.status == PlanItemStatus.DONE:
        if item.completed_at is None:
            item.completed_at = datetime.now(UTC)
    else:
        item.completed_at = None

    await session.flush()
    return item


@router.delete("/{plan_id}", response_model=Message)
async def delete_plan(plan_id: uuid.UUID, user: CurrentUserDep, session: DbSession) -> Message:
    plan = await session.get(DevelopmentPlan, plan_id)
    if plan is None or plan.user_id != uuid.UUID(user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    await session.delete(plan)
    return Message(detail="Plan deleted")
