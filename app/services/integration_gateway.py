"""Integration gateway for Edu-Job, Invest HUB and Tijorat markazi.

Two rules shape this module:
1. Nothing leaves the platform without a matching consent record.
2. Outbound calls go through a transactional outbox, so a partner being down
   never loses a user's application.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    ConsentScope,
    IntegrationEventStatus,
    IntegrationSystem,
)
from app.models.integration import IntegrationEvent
from app.services.audit_service import has_consent

logger = logging.getLogger(__name__)

CONSENT_FOR_SYSTEM: dict[IntegrationSystem, ConsentScope] = {
    IntegrationSystem.EDU_JOB: ConsentScope.SHARE_EDU_JOB,
    IntegrationSystem.INVEST_HUB: ConsentScope.SHARE_INVEST_HUB,
    IntegrationSystem.COMMERCE: ConsentScope.SHARE_COMMERCE,
}


class ConsentMissingError(PermissionError):
    """Raised when a transfer is attempted without the user's consent."""


@dataclass(slots=True)
class PartnerConfig:
    base_url: str | None
    client_id: str | None
    client_secret: str | None


def config_for(system: IntegrationSystem) -> PartnerConfig:
    mapping = {
        IntegrationSystem.EDU_JOB: PartnerConfig(
            settings.edu_job_base_url,
            settings.edu_job_client_id,
            settings.edu_job_client_secret,
        ),
        IntegrationSystem.INVEST_HUB: PartnerConfig(
            settings.invest_hub_base_url,
            settings.invest_hub_client_id,
            settings.invest_hub_client_secret,
        ),
        IntegrationSystem.COMMERCE: PartnerConfig(
            settings.commerce_base_url,
            settings.commerce_client_id,
            settings.commerce_client_secret,
        ),
    }
    return mapping[system]


def _idempotency_key(system: IntegrationSystem, event_type: str, ref: str) -> str:
    raw = f"{system.value}:{event_type}:{ref}"
    return hashlib.sha256(raw.encode()).hexdigest()[:60]


async def queue_event(
    session: AsyncSession,
    *,
    system: IntegrationSystem,
    event_type: str,
    payload: dict[str, Any],
    user_id: uuid.UUID | None = None,
    reference: str | None = None,
    require_consent: bool = True,
) -> IntegrationEvent:
    """Write an outbound event to the outbox in the caller's transaction."""
    scope = CONSENT_FOR_SYSTEM[system]

    if require_consent and user_id is not None and not await has_consent(session, user_id, scope):
        raise ConsentMissingError(f"user has not consented to sharing data with {system.value}")

    key = _idempotency_key(system, event_type, reference or str(uuid.uuid4()))

    existing = await session.scalar(
        select(IntegrationEvent).where(IntegrationEvent.idempotency_key == key)
    )
    if existing is not None:
        return existing

    event = IntegrationEvent(
        system=system,
        event_type=event_type,
        idempotency_key=key,
        user_id=user_id,
        payload=payload,
        consent_scope=scope.value,
        status=IntegrationEventStatus.PENDING,
        next_retry_at=datetime.now(UTC),
    )
    session.add(event)
    return event


# Event type -> partner endpoint, per the minimal API contract in section 05.
ENDPOINTS: dict[str, str] = {
    "user.sync": "/users/sync",
    "application.submit": "/applications",
    "result.push": "/results",
    "skills.sync": "/skills",
    "consent.sync": "/consent",
}


async def deliver(
    session: AsyncSession, event: IntegrationEvent, client: httpx.AsyncClient
) -> bool:
    """Attempt one delivery, applying exponential backoff on failure."""
    config = config_for(event.system)
    if not config.base_url:
        event.last_error = "partner base_url is not configured"
        event.status = IntegrationEventStatus.FAILED
        return False

    path = ENDPOINTS.get(event.event_type)
    if path is None:
        event.status = IntegrationEventStatus.DEAD_LETTER
        event.last_error = f"unknown event type {event.event_type}"
        return False

    try:
        response = await client.post(
            f"{config.base_url.rstrip('/')}{path}",
            json=event.payload,
            headers={
                "Idempotency-Key": event.idempotency_key,
                "X-Client-Id": config.client_id or "",
                "Content-Type": "application/json",
            },
            timeout=settings.integration_timeout_seconds,
        )
        event.response_code = response.status_code

        if response.is_success:
            event.status = IntegrationEventStatus.SENT
            event.delivered_at = datetime.now(UTC)
            event.last_error = None
            return True

        # 4xx other than 429 will not succeed on retry.
        if 400 <= response.status_code < 500 and response.status_code != 429:
            event.status = IntegrationEventStatus.DEAD_LETTER
            event.last_error = response.text[:500]
            return False

        raise httpx.HTTPStatusError(
            f"partner returned {response.status_code}",
            request=response.request,
            response=response,
        )

    except Exception as exc:
        event.retry_count += 1
        event.last_error = str(exc)[:500]
        if event.retry_count >= settings.integration_max_retries:
            event.status = IntegrationEventStatus.DEAD_LETTER
            logger.error(
                "integration event exhausted retries",
                extra={"extra_fields": {"event_id": str(event.id)}},
            )
        else:
            event.status = IntegrationEventStatus.PENDING
            backoff = timedelta(seconds=2 ** (event.retry_count + 2))
            event.next_retry_at = datetime.now(UTC) + backoff
        return False


async def drain_outbox(session: AsyncSession, limit: int = 50) -> tuple[int, int]:
    """Deliver due events. Returns (delivered, failed). Run by the worker."""
    now = datetime.now(UTC)
    due = list(
        (
            await session.execute(
                select(IntegrationEvent)
                .where(
                    IntegrationEvent.status == IntegrationEventStatus.PENDING,
                    (IntegrationEvent.next_retry_at.is_(None))
                    | (IntegrationEvent.next_retry_at <= now),
                )
                .limit(limit)
            )
        ).scalars()
    )
    if not due:
        return 0, 0

    delivered = failed = 0
    async with httpx.AsyncClient() as client:
        for event in due:
            if await deliver(session, event, client):
                delivered += 1
            else:
                failed += 1
    return delivered, failed


def minimal_profile_payload(user, profile) -> dict[str, Any]:
    """Data minimisation: the fields a partner needs, and nothing more.

    Notably absent: phone, email, full name, marital status, children count and
    register membership. Partners receive a pseudonymous id.
    """
    return {
        "womanup_id": str(user.id),
        "region": user.region.value if user.region else None,
        "language": user.language.value,
        "education_level": profile.education_level if profile else None,
        "employment_status": profile.employment_status if profile else None,
        "profession": profile.profession if profile else None,
        "skills": profile.skills if profile else [],
        "years_of_experience": profile.years_of_experience if profile else None,
    }
