"""Management dashboard: KPI, coverage, role administration, audit."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select

from app.api.deps import AdminDep, DbSession, StaffDep, coordinator_region
from app.core.constants import Region, Role
from app.models.audit import AuditLog
from app.models.user import User, UserRole
from app.schemas.admin import (
    DashboardFilter,
    DashboardOverview,
    KpiSnapshot,
    TrafficReport,
)
from app.schemas.common import Message
from app.schemas.user import UserRoleAssign
from app.services import traffic
from app.services.analytics import dashboard_overview, kpi_snapshot
from app.services.audit_service import record_audit

router = APIRouter(prefix="/admin", tags=["admin"])


async def _scoped_filters(session, user, filters: DashboardFilter) -> DashboardFilter:
    """A regional coordinator only ever sees their own region.

    Enforced server-side from her role assignment: overriding the query
    parameter cannot widen scope.
    """
    region = await coordinator_region(session, user)
    if region is not None:
        filters.region = region
    return filters


@router.get("/dashboard", response_model=DashboardOverview, deprecated=True)
async def overview(
    user: StaffDep,
    session: DbSession,
    filters: DashboardFilter = Depends(),
) -> DashboardOverview:
    return await dashboard_overview(session, await _scoped_filters(session, user, filters))


@router.get("/kpi", response_model=KpiSnapshot, deprecated=True)
async def kpi(
    user: StaffDep,
    session: DbSession,
    filters: DashboardFilter = Depends(),
) -> KpiSnapshot:
    """The MVP KPI set from section 09."""
    return await kpi_snapshot(session, await _scoped_filters(session, user, filters))


@router.get("/kpi/export", deprecated=True)
async def export_kpi(
    user: StaffDep,
    session: DbSession,
    filters: DashboardFilter = Depends(),
) -> Response:
    """CSV export for reporting to the Assembly."""
    snapshot = await kpi_snapshot(session, await _scoped_filters(session, user, filters))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["key", "label", "value", "unit", "numerator", "denominator", "target"])
    for item in snapshot.kpis:
        writer.writerow(
            [
                item.key,
                item.label,
                item.value,
                item.unit,
                item.numerator,
                item.denominator,
                item.target,
            ]
        )

    await record_audit(
        session,
        action="kpi.export",
        entity_type="report",
        actor_id=uuid.UUID(user.id),
        actor_role=user.role.value,
    )

    filename = f"womanup-kpi-{date.today():%Y%m%d}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/users", response_model=list[dict])
async def list_users(
    user: StaffDep,
    session: DbSession,
    region: Region | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Operational user list. Deliberately returns no direct identifiers —
    coordinators work with regions and activity, not phone numbers."""
    stmt = select(User)
    scoped = await coordinator_region(session, user)
    if scoped is not None:
        stmt = stmt.where(User.region == scoped)
    elif region:
        stmt = stmt.where(User.region == region)

    rows = await session.execute(
        stmt.order_by(User.created_at.desc()).offset(offset).limit(min(limit, 200))
    )
    return [
        {
            "id": str(record.id),
            "status": record.status.value,
            "region": record.region.value if record.region else None,
            "language": record.language.value,
            "onboarded": record.onboarding_completed_at is not None,
            "last_active_at": record.last_active_at.isoformat() if record.last_active_at else None,
            "created_at": record.created_at.isoformat(),
        }
        for record in rows.scalars()
    ]


@router.post("/users/{user_id}/roles", response_model=Message)
async def assign_role(
    user_id: uuid.UUID,
    payload: UserRoleAssign,
    user: AdminDep,
    session: DbSession,
) -> Message:
    """Grant a role. Admin only, and always audited."""
    if await session.get(User, user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    existing = await session.scalar(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role == payload.role)
    )
    if existing is not None:
        existing.scope_region = payload.scope_region
    else:
        session.add(
            UserRole(
                user_id=user_id,
                role=payload.role,
                scope_region=payload.scope_region,
                granted_by_id=uuid.UUID(user.id),
            )
        )

    await record_audit(
        session,
        action="role.assign",
        entity_type="user",
        entity_id=str(user_id),
        actor_id=uuid.UUID(user.id),
        actor_role=user.role.value,
        changes={
            "role": payload.role.value,
            "scope_region": payload.scope_region.value if payload.scope_region else None,
        },
    )
    return Message(detail=f"Role '{payload.role.value}' assigned")


@router.delete("/users/{user_id}/roles/{role}", response_model=Message)
async def revoke_role(
    user_id: uuid.UUID, role: Role, user: AdminDep, session: DbSession
) -> Message:
    assignment = await session.scalar(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role == role)
    )
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role assignment not found"
        )
    await session.delete(assignment)

    await record_audit(
        session,
        action="role.revoke",
        entity_type="user",
        entity_id=str(user_id),
        actor_id=uuid.UUID(user.id),
        actor_role=user.role.value,
        changes={"role": role.value},
    )
    return Message(detail=f"Role '{role.value}' revoked")


@router.get("/audit", response_model=list[dict])
async def read_audit_log(
    user: AdminDep,
    session: DbSession,
    entity_type: str | None = None,
    action: str | None = None,
    limit: int = 100,
) -> list[dict]:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if action:
        stmt = stmt.where(AuditLog.action == action)

    rows = await session.execute(stmt.limit(min(limit, 500)))
    return [
        {
            "id": str(entry.id),
            "actor_id": str(entry.actor_id) if entry.actor_id else None,
            "actor_role": entry.actor_role,
            "action": entry.action,
            "entity_type": entry.entity_type,
            "entity_id": entry.entity_id,
            "classification": entry.classification.value,
            "changes": entry.changes,
            "request_id": entry.request_id,
            "created_at": entry.created_at.isoformat(),
        }
        for entry in rows.scalars()
    ]


@router.get("/traffic", response_model=TrafficReport)
async def site_traffic(user: StaffDep, session: DbSession, days: int = 30) -> TrafficReport:
    """Visits, not achievements: how many people opened the site at all.

    Counted from `page_views`, which includes visitors with no account — most
    of the traffic before someone registers, and invisible to every other
    number on this dashboard.
    """
    report = await traffic.report(session, days=min(max(days, 1), 90))
    return TrafficReport.model_validate(report, from_attributes=True)


@router.get("/stats/regions", response_model=list[dict])
async def region_stats(user: StaffDep, session: DbSession) -> list[dict]:
    rows = await session.execute(
        select(User.region, func.count(User.id))
        .where(User.region.is_not(None))
        .group_by(User.region)
        .order_by(func.count(User.id).desc())
    )
    return [{"region": region.value, "users": count} for region, count in rows]
