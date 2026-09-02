"""Recording and reporting site traffic.

Kept apart from `analytics.py`, which answers "what did our users achieve".
This one answers "did anyone come at all" — a different question with a
different privacy profile, since most of the people it counts have no account.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.analytics import PageView

# Anything longer is a bug or an attack, not a route.
MAX_PATH = 120

# Routes we are willing to store. Anything else is recorded as "other", so a
# stray or crafted path can never turn this table into a free-text log.
KNOWN_PREFIXES: tuple[str, ...] = (
    "/",
    "/login",
    "/welcome",
    "/kabinet",
    "/diagnostika",
    "/reja",
    "/dasturlar",
    "/imkoniyatlar",
    "/yordamchi",
    "/navigator",
    "/admin",
)


def hash_visitor(visitor_id: str) -> str:
    """HMAC the first-party cookie id under the app secret.

    The raw id never lands in the database. Someone reading `page_views` can
    tell that two rows are the same person; they cannot tell which person, and
    cannot recognise her anywhere else.
    """
    return hmac.new(
        settings.jwt_secret_key.encode(),
        visitor_id.encode(),
        hashlib.sha256,
    ).hexdigest()


def normalise_path(raw: str) -> str:
    """Route only: no query string, no fragment, no unexpected paths."""
    path = (raw or "/").split("?")[0].split("#")[0][:MAX_PATH]
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return path if path in KNOWN_PREFIXES else "other"


async def record_view(
    session: AsyncSession,
    *,
    visitor_id: str,
    path: str,
    user_id: uuid.UUID | None,
    locale: str | None,
) -> None:
    """Write one view. Cheap enough to call on every navigation."""
    session.add(
        PageView(
            visitor_hash=hash_visitor(visitor_id),
            user_id=user_id,
            path=normalise_path(path),
            locale=(locale or "")[:10] or None,
            is_authenticated=user_id is not None,
            occurred_at=datetime.now(UTC),
        )
    )


# ------------------------------------------------------------------ reporting


@dataclass(slots=True)
class DayPoint:
    day: date
    visitors: int
    views: int


@dataclass(slots=True)
class PathPoint:
    path: str
    views: int
    visitors: int


@dataclass(slots=True)
class TrafficReport:
    visitors_today: int
    visitors_7d: int
    visitors_30d: int
    views_today: int
    views_30d: int
    signed_in_share: float
    returning_share: float
    by_day: list[DayPoint]
    top_paths: list[PathPoint]


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


async def _unique(session: AsyncSession, stmt: Select) -> int:
    return await session.scalar(stmt) or 0


async def report(session: AsyncSession, *, days: int = 30) -> TrafficReport:
    """Everything the dashboard's traffic card needs, in one pass per metric."""
    start_of_today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    visitors_today = await _unique(
        session,
        select(func.count(func.distinct(PageView.visitor_hash))).where(
            PageView.occurred_at >= start_of_today
        ),
    )
    visitors_7d = await _unique(
        session,
        select(func.count(func.distinct(PageView.visitor_hash))).where(
            PageView.occurred_at >= _since(7)
        ),
    )
    visitors_30d = await _unique(
        session,
        select(func.count(func.distinct(PageView.visitor_hash))).where(
            PageView.occurred_at >= _since(days)
        ),
    )
    views_today = await _unique(
        session,
        select(func.count(PageView.id)).where(PageView.occurred_at >= start_of_today),
    )
    views_30d = await _unique(
        session,
        select(func.count(PageView.id)).where(PageView.occurred_at >= _since(days)),
    )

    signed_in = await _unique(
        session,
        select(func.count(func.distinct(PageView.visitor_hash))).where(
            PageView.occurred_at >= _since(days),
            PageView.is_authenticated.is_(True),
        ),
    )

    # A returning visitor is one seen on more than one day in the window: the
    # single number that separates "people came back" from "a campaign ran".
    per_visitor_days = (
        select(func.count(func.distinct(func.date(PageView.occurred_at))).label("days"))
        .where(PageView.occurred_at >= _since(days))
        .group_by(PageView.visitor_hash)
        .subquery()
    )
    returning = (
        await session.scalar(
            select(func.count()).select_from(per_visitor_days).where(per_visitor_days.c.days > 1)
        )
        or 0
    )

    day_rows = await session.execute(
        select(
            func.date(PageView.occurred_at).label("day"),
            func.count(func.distinct(PageView.visitor_hash)),
            func.count(PageView.id),
        )
        .where(PageView.occurred_at >= _since(days))
        .group_by("day")
        .order_by("day")
    )
    path_rows = await session.execute(
        select(
            PageView.path,
            func.count(PageView.id),
            func.count(func.distinct(PageView.visitor_hash)),
        )
        .where(PageView.occurred_at >= _since(days))
        .group_by(PageView.path)
        .order_by(func.count(PageView.id).desc())
        .limit(10)
    )

    return TrafficReport(
        visitors_today=visitors_today,
        visitors_7d=visitors_7d,
        visitors_30d=visitors_30d,
        views_today=views_today,
        views_30d=views_30d,
        signed_in_share=round(signed_in * 100 / visitors_30d, 1) if visitors_30d else 0.0,
        returning_share=round(returning * 100 / visitors_30d, 1) if visitors_30d else 0.0,
        by_day=[DayPoint(day=row[0], visitors=row[1], views=row[2]) for row in day_rows],
        top_paths=[PathPoint(path=row[0], views=row[1], visitors=row[2]) for row in path_rows],
    )
