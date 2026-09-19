"""Results & impact: the admin dashboard's figures, computed on the server.

Staff only (administrators, regional coordinators, moderators — the roles the
panel already serves). A regional coordinator's figures are her region's,
whatever region she asks for: the scope is read from her role assignment in the
database, not from the browser, and a coordinator nobody has given a region is
refused rather than shown the whole country. Definitions of every figure live
in `services.results.DEFINITIONS`.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import DbSession, StaffDep, coordinator_region
from app.core.constants import Region, Role
from app.schemas.results import (
    ProgrammeTable,
    ResultsEvents,
    ResultsLearning,
    ResultsOpportunities,
    ResultsOverview,
    ResultsScore,
    ResultsSkills,
)
from app.services import results
from app.services.audit_service import record_audit

router = APIRouter(prefix="/admin/results", tags=["admin"])


async def _filters(
    session, user, date_from: date | None, date_to: date | None, region: Region | None
) -> results.Filters:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "period_reversed"},
        )
    if not user.has_role(Role.ADMIN, Role.MODERATOR):
        scoped = await coordinator_region(session, user)
        if scoped is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail={"reason": "no_region_scope"}
            )
        region = scoped
    return results.Filters(date_from=date_from, date_to=date_to, region=region)


_PERIOD = {
    "date_from": Query(default=None, description="First day, inclusive (Tashkent)."),
    "date_to": Query(default=None, description="Last day, inclusive (Tashkent)."),
}


@router.get("/overview", response_model=ResultsOverview)
async def overview(
    user: StaffDep,
    session: DbSession,
    date_from: date | None = _PERIOD["date_from"],
    date_to: date | None = _PERIOD["date_to"],
    region: Region | None = None,
) -> ResultsOverview:
    """Who is here, what they did in the period, and where they are from."""
    return await results.overview(
        session, await _filters(session, user, date_from, date_to, region)
    )


@router.get("/learning", response_model=ResultsLearning)
async def learning(
    user: StaffDep,
    session: DbSession,
    date_from: date | None = _PERIOD["date_from"],
    date_to: date | None = _PERIOD["date_to"],
    region: Region | None = None,
) -> ResultsLearning:
    """Courses, learning paths, practical tasks and certificates."""
    return await results.learning(
        session, await _filters(session, user, date_from, date_to, region)
    )


@router.get("/programmes", response_model=ProgrammeTable)
async def programmes(
    user: StaffDep,
    session: DbSession,
    date_from: date | None = _PERIOD["date_from"],
    date_to: date | None = _PERIOD["date_to"],
    region: Region | None = None,
    search: str | None = Query(default=None, max_length=80),
    sort: str = Query(
        default="enrolled", pattern="^(enrolled|completed|completion|certificates|title)$"
    ),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=50),
    lang: str = Query(default="uz", pattern="^(uz|ru|en)$", description="Language names sort in."),
) -> ProgrammeTable:
    """Each programme's figures for the period, sorted and paged on the server."""
    return await results.programmes(
        session,
        await _filters(session, user, date_from, date_to, region),
        search=search,
        sort=sort,
        descending=order == "desc",
        page=page,
        size=size,
        lang=lang,
    )


@router.get("/skills", response_model=ResultsSkills)
async def skills(
    user: StaffDep,
    session: DbSession,
    date_from: date | None = _PERIOD["date_from"],
    date_to: date | None = _PERIOD["date_to"],
    region: Region | None = None,
) -> ResultsSkills:
    """Skill evidence by what it supports — learned, assessed, verified — kept apart."""
    return await results.skills(session, await _filters(session, user, date_from, date_to, region))


@router.get("/score", response_model=ResultsScore)
async def score(
    user: StaffDep,
    session: DbSession,
    region: Region | None = None,
) -> ResultsScore:
    """Development Score as it stands now, with its sample size."""
    return await results.score(session, await _filters(session, user, None, None, region))


@router.get("/opportunities", response_model=ResultsOpportunities)
async def opportunities(
    user: StaffDep,
    session: DbSession,
    date_from: date | None = _PERIOD["date_from"],
    date_to: date | None = _PERIOD["date_to"],
    region: Region | None = None,
) -> ResultsOpportunities:
    """Applications, saved listings, organisations' invitations and recorded outcomes."""
    return await results.opportunities(
        session, await _filters(session, user, date_from, date_to, region)
    )


@router.get("/events", response_model=ResultsEvents)
async def events(
    user: StaffDep,
    session: DbSession,
    date_from: date | None = _PERIOD["date_from"],
    date_to: date | None = _PERIOD["date_to"],
    region: Region | None = None,
) -> ResultsEvents:
    """Events added and held, registrations, reminders. Attendance is not recorded."""
    return await results.events(session, await _filters(session, user, date_from, date_to, region))


@router.get("/export")
async def export(
    user: StaffDep,
    session: DbSession,
    date_from: date | None = _PERIOD["date_from"],
    date_to: date | None = _PERIOD["date_to"],
    region: Region | None = None,
) -> Response:
    """The headline figures and their definitions as CSV, for a report."""
    f = await _filters(session, user, date_from, date_to, region)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["period_from", f.date_from or "all", "period_to", f.date_to or "all"])
    writer.writerow(["region", f.region.value if f.region else "all"])
    writer.writerow([])
    writer.writerow(["metric", "value", "numerator", "denominator", "definition"])
    for row in await results.export_rows(session, f):
        writer.writerow(row)

    await record_audit(
        session,
        action="results.export",
        entity_type="report",
        actor_id=uuid.UUID(user.id),
        actor_role=user.role.value,
        changes={
            "date_from": str(f.date_from) if f.date_from else None,
            "date_to": str(f.date_to) if f.date_to else None,
            "region": f.region.value if f.region else None,
        },
    )
    name = f"womanup-results-{f.date_from or 'all'}-{f.date_to or 'all'}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
