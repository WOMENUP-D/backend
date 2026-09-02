"""Inbound webhooks and outbound sync with partner platforms."""

from __future__ import annotations

import hmac
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import AdminDep, CurrentUserDep, DbSession, StaffDep
from app.core.constants import (
    ApplicationStatus,
    DataClassification,
    IntegrationSystem,
    OpportunitySource,
)
from app.models.integration import IntegrationEvent
from app.models.opportunity import Application, Opportunity, OutcomeRecord
from app.models.profile import Profile
from app.models.user import User
from app.schemas.common import Message
from app.schemas.opportunity import (
    ApplicationStatusUpdate,
    OutcomeCreate,
    OutcomeRead,
)
from app.services.audit_service import record_audit
from app.services.integration_gateway import (
    ConsentMissingError,
    config_for,
    drain_outbox,
    minimal_profile_payload,
    queue_event,
)

router = APIRouter(prefix="/integrations", tags=["integrations"])

SYSTEM_TO_SOURCE = {
    IntegrationSystem.EDU_JOB: OpportunitySource.EDU_JOB,
    IntegrationSystem.INVEST_HUB: OpportunitySource.INVEST_HUB,
    IntegrationSystem.COMMERCE: OpportunitySource.COMMERCE,
}


def _verify_partner(system: IntegrationSystem, client_id: str | None) -> None:
    """Authenticate an inbound callback.

    A shared client id compared in constant time is the minimum bar; before
    production this should become OAuth2 client credentials or a signed
    webhook, per section 05.
    """
    config = config_for(system)
    if not config.client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Integration with {system.value} is not configured",
        )
    if not client_id or not hmac.compare_digest(client_id, config.client_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid partner credentials"
        )


@router.post("/{system}/applications/{application_id}/status", response_model=Message)
async def receive_application_status(
    system: IntegrationSystem,
    application_id: uuid.UUID,
    payload: ApplicationStatusUpdate,
    session: DbSession,
    request: Request,
    x_client_id: str | None = Header(default=None),
) -> Message:
    """A partner reports an application's new status back into the cabinet."""
    _verify_partner(system, x_client_id)

    application = await session.get(Application, application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")

    now = datetime.now(UTC)
    application.status = payload.status
    if payload.external_application_id:
        application.external_application_id = payload.external_application_id
    if payload.status in (ApplicationStatus.ACCEPTED, ApplicationStatus.REJECTED):
        application.resolved_at = now
    application.status_history = [
        *application.status_history,
        {"status": payload.status.value, "at": now.isoformat(), "note": payload.note},
    ]

    await record_audit(
        session,
        action="integration.application_status",
        entity_type="application",
        entity_id=str(application_id),
        classification=DataClassification.INTERNAL,
        changes={"status": payload.status.value, "system": system.value},
    )
    return Message(detail="Status updated")


@router.post("/{system}/outcomes", response_model=OutcomeRead, status_code=status.HTTP_201_CREATED)
async def receive_outcome(
    system: IntegrationSystem,
    payload: OutcomeCreate,
    user_id: uuid.UUID,
    session: DbSession,
    x_client_id: str | None = Header(default=None),
) -> OutcomeRecord:
    """A partner confirms a real result — hired, funded, first sale.

    This is what closes the loop: the result returns to the cabinet and feeds
    the North Star metric.
    """
    _verify_partner(system, x_client_id)

    if await session.get(User, user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    outcome = OutcomeRecord(
        user_id=user_id,
        application_id=payload.application_id,
        source=SYSTEM_TO_SOURCE[system],
        outcome_type=payload.outcome_type,
        details=payload.details,
        verified=True,
        occurred_at=payload.occurred_at or datetime.now(UTC),
    )
    session.add(outcome)

    await record_audit(
        session,
        action="integration.outcome_received",
        entity_type="outcome",
        entity_id=str(user_id),
        classification=DataClassification.INTERNAL,
        changes={"type": payload.outcome_type, "system": system.value},
    )
    await session.flush()
    return outcome


@router.post("/{system}/opportunities/sync", response_model=Message)
async def sync_opportunities(
    system: IntegrationSystem,
    items: list[dict],
    session: DbSession,
    x_client_id: str | None = Header(default=None),
) -> Message:
    """Upsert an opportunity batch from a partner.

    Keyed on (source, external_id), so a partner re-sending the same batch
    updates rather than duplicates.
    """
    _verify_partner(system, x_client_id)
    source = SYSTEM_TO_SOURCE[system]

    created = updated = 0
    for item in items:
        external_id = str(item.get("external_id") or "").strip()
        if not external_id:
            continue

        existing = await session.scalar(
            select(Opportunity).where(
                Opportunity.source == source, Opportunity.external_id == external_id
            )
        )
        fields = {
            "type": item.get("type", "vacancy"),
            "title": item.get("title", "")[:300],
            "description": item.get("description"),
            "organisation": item.get("organisation"),
            "region": item.get("region"),
            "required_skills": item.get("required_skills", []),
            "eligibility": item.get("eligibility", {}),
            "reward": item.get("reward", {}),
            "external_url": item.get("url"),
            "is_active": item.get("is_active", True),
            "synced_at": datetime.now(UTC),
        }

        if existing is not None:
            for key, value in fields.items():
                setattr(existing, key, value)
            updated += 1
        else:
            session.add(Opportunity(source=source, external_id=external_id, **fields))
            created += 1

    return Message(detail=f"{created} created, {updated} updated")


@router.post("/sync-profile", response_model=Message)
async def sync_my_profile(
    system: IntegrationSystem, user: CurrentUserDep, session: DbSession
) -> Message:
    """Push the user's minimal profile to a partner she has consented to."""
    user_id = uuid.UUID(user.id)
    record = await session.get(User, user_id)
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))

    try:
        await queue_event(
            session,
            system=system,
            event_type="user.sync",
            user_id=user_id,
            reference=f"{user_id}:{system.value}",
            payload=minimal_profile_payload(record, profile),
        )
    except ConsentMissingError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    return Message(detail=f"Profile queued for sync with {system.value}")


@router.post("/outbox/drain", response_model=Message)
async def drain(user: AdminDep, session: DbSession, limit: int = 50) -> Message:
    """Manually flush the outbox. The worker does this on a schedule; the
    endpoint exists for operational recovery."""
    delivered, failed = await drain_outbox(session, limit=limit)
    return Message(detail=f"{delivered} delivered, {failed} failed")


@router.get("/outbox/failed", response_model=list[dict])
async def failed_events(user: StaffDep, session: DbSession) -> list[dict]:
    """Dead-lettered events awaiting manual intervention."""
    from app.core.constants import IntegrationEventStatus

    rows = await session.execute(
        select(IntegrationEvent)
        .where(IntegrationEvent.status == IntegrationEventStatus.DEAD_LETTER)
        .order_by(IntegrationEvent.created_at.desc())
        .limit(100)
    )
    return [
        {
            "id": str(event.id),
            "system": event.system.value,
            "event_type": event.event_type,
            "retry_count": event.retry_count,
            "last_error": event.last_error,
            "created_at": event.created_at.isoformat(),
        }
        for event in rows.scalars()
    ]
