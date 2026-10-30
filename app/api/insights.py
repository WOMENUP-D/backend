"""Product analytics for the management panel.

Administrators and moderators only: these are national figures — traffic,
funnels, errors — with no region to scope a coordinator to (she has the
Results dashboard, which scopes everything). Every endpoint takes the same
period, cut on Tashkent days, defaulting to the last 30 and refusing more than
a year; every answer says when it was computed and whether it came from the
one-minute cache. Definitions of every figure are in
`services.insights.DEFINITIONS` and are served alongside.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import DbSession, NationalStaffDep
from app.schemas.insights import (
    Accounts,
    Activity,
    Engagement,
    Errors,
    Features,
    Funnel,
    Live,
    Overview,
    Programs,
    Traffic,
)
from app.services import insights

router = APIRouter(prefix="/admin/analytics", tags=["admin"])

_FROM = Query(default=None, description="First day, inclusive (Tashkent).")
_TO = Query(default=None, description="Last day, inclusive (Tashkent).")
_STAFF = Query(default=False, description="Count staff accounts' own browsing and actions too.")


def _query(date_from: date | None, date_to: date | None, include_staff: bool) -> insights.Query:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"reason": "period_reversed"}
        )
    try:
        filters = insights.filters_for(date_from, date_to)
    except insights.PeriodTooLong:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"reason": "period_too_long"}
        ) from None
    return insights.Query(filters=filters, include_staff=include_staff)


@router.get("/definitions", response_model=dict[str, str])
async def definitions(user: NationalStaffDep) -> dict[str, str]:
    """What every figure counts, in words."""
    return insights.DEFINITIONS


@router.get("/live", response_model=Live)
async def live(user: NationalStaffDep, session: DbSession, include_staff: bool = _STAFF) -> Live:
    """Who is here right now. Never cached."""
    return await insights.live(session, _query(None, None, include_staff))


@router.get("/overview", response_model=Overview)
async def overview(
    user: NationalStaffDep,
    session: DbSession,
    date_from: date | None = _FROM,
    date_to: date | None = _TO,
    include_staff: bool = _STAFF,
) -> Overview:
    return await insights.overview(session, _query(date_from, date_to, include_staff))


@router.get("/traffic", response_model=Traffic)
async def traffic(
    user: NationalStaffDep,
    session: DbSession,
    date_from: date | None = _FROM,
    date_to: date | None = _TO,
    include_staff: bool = _STAFF,
) -> Traffic:
    return await insights.traffic(session, _query(date_from, date_to, include_staff))


@router.get("/accounts", response_model=Accounts)
async def accounts(
    user: NationalStaffDep,
    session: DbSession,
    date_from: date | None = _FROM,
    date_to: date | None = _TO,
    include_staff: bool = _STAFF,
) -> Accounts:
    return await insights.accounts(session, _query(date_from, date_to, include_staff))


@router.get("/engagement", response_model=Engagement)
async def engagement(
    user: NationalStaffDep,
    session: DbSession,
    date_from: date | None = _FROM,
    date_to: date | None = _TO,
    include_staff: bool = _STAFF,
) -> Engagement:
    return await insights.engagement(session, _query(date_from, date_to, include_staff))


@router.get("/funnel", response_model=Funnel)
async def funnel(
    user: NationalStaffDep,
    session: DbSession,
    date_from: date | None = _FROM,
    date_to: date | None = _TO,
    include_staff: bool = _STAFF,
) -> Funnel:
    return await insights.funnel(session, _query(date_from, date_to, include_staff))


@router.get("/programs", response_model=Programs)
async def programs(
    user: NationalStaffDep,
    session: DbSession,
    date_from: date | None = _FROM,
    date_to: date | None = _TO,
    include_staff: bool = _STAFF,
    search: str | None = Query(default=None, max_length=80),
    sort: str = Query(
        default="enrolled", pattern="^(enrolled|started|completed|completion|views|stalled|title)$"
    ),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=50),
    lang: str = Query(default="uz", pattern="^(uz|ru|en)$"),
) -> Programs:
    return await insights.programs(
        session,
        _query(date_from, date_to, include_staff),
        sort=sort,
        descending=order == "desc",
        page=page,
        size=size,
        search=search,
        lang=lang,
    )


@router.get("/features", response_model=Features)
async def features(
    user: NationalStaffDep,
    session: DbSession,
    date_from: date | None = _FROM,
    date_to: date | None = _TO,
    include_staff: bool = _STAFF,
) -> Features:
    return await insights.features(session, _query(date_from, date_to, include_staff))


@router.get("/activity", response_model=Activity)
async def activity(
    user: NationalStaffDep,
    session: DbSession,
    date_from: date | None = _FROM,
    date_to: date | None = _TO,
    include_staff: bool = _STAFF,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=30, ge=1, le=100),
    names: list[str] | None = Query(default=None),
) -> Activity:
    return await insights.activity(
        session, _query(date_from, date_to, include_staff), page=page, size=size, names=names
    )


@router.get("/errors", response_model=Errors)
async def errors(
    user: NationalStaffDep,
    session: DbSession,
    date_from: date | None = _FROM,
    date_to: date | None = _TO,
    include_staff: bool = _STAFF,
) -> Errors:
    return await insights.errors(session, _query(date_from, date_to, include_staff))
