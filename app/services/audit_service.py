"""Audit and consent helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ConsentScope, DataClassification
from app.core.logging import request_id_ctx
from app.models.audit import AuditLog
from app.models.consent import ConsentLog


async def record_audit(
    session: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    actor_role: str | None = None,
    classification: DataClassification = DataClassification.INTERNAL,
    changes: dict | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Append an audit entry. Never updates an existing row."""
    session.add(
        AuditLog(
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            classification=classification,
            changes=changes or {},
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id_ctx.get(),
        )
    )


async def record_consent(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    scope: ConsentScope,
    accepted: bool,
    policy_version: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    subject_ref: str | None = None,
) -> ConsentLog:
    now = datetime.now(UTC)
    entry = ConsentLog(
        user_id=user_id,
        scope=scope,
        accepted=accepted,
        policy_version=policy_version,
        accepted_at=now,
        ip_address=ip_address,
        user_agent=user_agent,
        subject_ref=subject_ref,
    )
    # Stamped with the moment she answered rather than the start of the
    # transaction, so "latest row wins" orders two answers correctly even when
    # they land in one transaction — a yes and then a no must never tie.
    entry.created_at = now
    session.add(entry)
    return entry


async def has_consent(
    session: AsyncSession,
    user_id: uuid.UUID,
    scope: ConsentScope,
    subject_ref: str | None = None,
) -> bool:
    """Current consent state = the most recent entry for that scope — and, for a
    scope given to one party, for that party. A consent to one organisation is
    never read as a consent to another, or as a platform-wide one."""
    subject = (
        ConsentLog.subject_ref.is_(None)
        if subject_ref is None
        else ConsentLog.subject_ref == subject_ref
    )
    latest = await session.scalar(
        select(ConsentLog.accepted)
        .where(ConsentLog.user_id == user_id, ConsentLog.scope == scope, subject)
        .order_by(desc(ConsentLog.created_at))
        .limit(1)
    )
    return bool(latest)


async def current_consents(session: AsyncSession, user_id: uuid.UUID) -> dict[ConsentScope, bool]:
    """Latest state for every platform-wide scope the user has ever answered.

    Scoped consents (shared with one organisation) are left out: "yes" to one
    organisation is not "yes" to the scope.
    """
    rows = await session.execute(
        select(ConsentLog)
        .where(ConsentLog.user_id == user_id, ConsentLog.subject_ref.is_(None))
        .order_by(ConsentLog.scope, desc(ConsentLog.created_at))
    )
    result: dict[ConsentScope, bool] = {}
    for entry in rows.scalars():
        result.setdefault(entry.scope, entry.accepted)
    return result
