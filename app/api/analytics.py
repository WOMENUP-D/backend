"""Traffic and event collection.

Open to anonymous callers by necessity: the traffic worth counting is mostly
people who have not signed up yet. That makes the write path the exposed one,
so it accepts a fixed vocabulary and nothing else — a route folded to a known
screen, an event name from a list, a handful of short properties — stores no
IP or user agent, and caps how often one visitor can add rows.

Three endpoints, one visitor cookie:

* `POST /analytics/view` — one screen opened. Also refreshes presence.
* `POST /analytics/event` — one of the few steps only the browser can see
  (`services.telemetry.CLIENT_EVENTS`). Business outcomes are never accepted
  from here; the server records those beside the records themselves.
* `POST /analytics/heartbeat` — "still here", once a minute from a visible
  tab, so "online now" means a person in front of an open page.
"""

from __future__ import annotations

import secrets
import time
import uuid
from typing import Any

from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import DbSession, OptionalUserDep
from app.core.config import settings
from app.schemas.common import Message
from app.services import telemetry, traffic
from app.services.traffic import hash_visitor

router = APIRouter(prefix="/analytics", tags=["analytics"])

VISITOR_COOKIE = telemetry.VISITOR_COOKIE
COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# One visitor may register a view at most this often. Navigation is bursty and
# a person legitimately opens several screens a minute, so the limit only has
# to stop a loop hammering the table.
MIN_SECONDS_BETWEEN_VIEWS = 1.0
MIN_SECONDS_BETWEEN_EVENTS = 0.25
# The browser sends one every sixty seconds; anything much faster is a bug.
MIN_SECONDS_BETWEEN_HEARTBEATS = 20.0
# And however spaced, this many rows a minute from one visitor is not a person.
MAX_EVENTS_PER_MINUTE = 60


class Throttle:
    """Per-visitor spacing and a per-minute ceiling, in process.

    Like the assistant's guest quota: it resets on restart and does not span
    workers. Good enough to stop a runaway client; move to Redis when the
    portal runs more than one worker.
    """

    def __init__(self, spacing: float, per_minute: int | None = None) -> None:
        self._spacing = spacing
        self._per_minute = per_minute
        self._last: dict[str, float] = {}
        self._minute: dict[str, list[float]] = {}
        self._max_tracked = 20_000

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        previous = self._last.get(key)
        if previous is not None and now - previous < self._spacing:
            return False
        if self._per_minute is not None:
            recent = [t for t in self._minute.get(key, []) if now - t < 60]
            if len(recent) >= self._per_minute:
                self._minute[key] = recent
                return False
            recent.append(now)
            self._minute[key] = recent
        if len(self._last) > self._max_tracked:
            self._last.clear()
            self._minute.clear()
        self._last[key] = now
        return True


_views = Throttle(MIN_SECONDS_BETWEEN_VIEWS)
_events = Throttle(MIN_SECONDS_BETWEEN_EVENTS, MAX_EVENTS_PER_MINUTE)
_heartbeats = Throttle(MIN_SECONDS_BETWEEN_HEARTBEATS)


class ViewIn(BaseModel):
    path: str = Field(max_length=200)
    locale: str | None = Field(default=None, max_length=10)
    #: The browser's session id; see `services.telemetry.normalise_session`.
    session: str | None = Field(default=None, max_length=40)
    #: True on the first view of a session — the landing page.
    entry: bool = False
    #: `document.referrer`, sent on the entry view only. Reduced to its host.
    referrer: str | None = Field(default=None, max_length=500)


class EventIn(BaseModel):
    name: str = Field(min_length=3, max_length=40)
    path: str | None = Field(default=None, max_length=200)
    locale: str | None = Field(default=None, max_length=10)
    session: str | None = Field(default=None, max_length=40)
    entity_type: str | None = Field(default=None, max_length=30)
    entity_id: str | None = Field(default=None, max_length=64)
    properties: dict[str, Any] | None = None


class HeartbeatIn(BaseModel):
    path: str = Field(max_length=200)
    session: str | None = Field(default=None, max_length=40)


def _own_hosts() -> frozenset[str]:
    hosts = set()
    for origin in settings.cors_origins:
        host = origin.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        if host:
            hosts.add(host.removeprefix("www."))
    hosts.update({"womanup.uz"})
    return frozenset(hosts)


def _visitor(response: Response, wu_vid: str | None) -> str:
    """The visitor id from the cookie, or a fresh one handed out now.

    First-party, no third party ever sees it, and only its HMAC is stored.
    """
    if wu_vid and 8 <= len(wu_vid) <= 64:
        return wu_vid
    visitor_id = secrets.token_urlsafe(16)
    response.set_cookie(
        VISITOR_COOKIE,
        visitor_id,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )
    return visitor_id


@router.post("/view", response_model=Message)
async def record_view(
    payload: ViewIn,
    request: Request,
    response: Response,
    session: DbSession,
    user: OptionalUserDep,
    wu_vid: str | None = Cookie(default=None),
    user_agent: str | None = Header(default=None),
) -> Message:
    """Record one screen opening. Fire-and-forget from the client's side."""
    visitor_id = _visitor(response, wu_vid)
    if not _views.allow(visitor_id):
        return Message(detail="throttled")

    user_id: uuid.UUID | None = uuid.UUID(user.id) if user is not None else None
    is_staff = telemetry.is_staff_user(user)
    client = telemetry.classify_user_agent(user_agent)
    session_id = telemetry.normalise_session(payload.session)

    await traffic.record_view(
        session,
        visitor_id=visitor_id,
        path=payload.path,
        user_id=user_id,
        locale=payload.locale,
        session_id=session_id,
        is_entry=payload.entry and session_id is not None,
        referrer_host=(
            telemetry.referrer_host(payload.referrer, own_hosts=_own_hosts())
            if payload.entry
            else None
        ),
        device=client.device,
        browser=client.browser,
        os=client.os,
        is_staff=is_staff,
    )
    if client.device != "bot":
        await telemetry.touch_presence(
            session,
            visitor_hash=hash_visitor(visitor_id),
            user_id=user_id,
            path=payload.path,
            is_staff=is_staff,
        )
    await session.commit()
    return Message(detail="recorded")


@router.post("/event", response_model=Message)
async def record_event(
    payload: EventIn,
    response: Response,
    session: DbSession,
    user: OptionalUserDep,
    wu_vid: str | None = Cookie(default=None),
) -> Message:
    """Record one step the browser saw. Names outside the list are refused."""
    if payload.name not in telemetry.CLIENT_EVENTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "unknown_event"},
        )
    try:
        entity_type, entity_id = telemetry.clean_entity(payload.entity_type, payload.entity_id)
    except telemetry.EventRejected:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"reason": "invalid_entity"}
        ) from None

    visitor_id = _visitor(response, wu_vid)
    if not _events.allow(visitor_id):
        return Message(detail="throttled")

    actor = telemetry.Actor(
        user_id=uuid.UUID(user.id) if user is not None else None,
        visitor_hash=hash_visitor(visitor_id),
        session_id=telemetry.normalise_session(payload.session),
        is_staff=telemetry.is_staff_user(user),
    )
    await telemetry.record_event(
        session,
        name=payload.name,
        actor=actor,
        path=payload.path,
        entity_type=entity_type,
        entity_id=entity_id,
        properties=telemetry.clean_properties(payload.properties),
        source="client",
        locale=payload.locale,
    )
    await session.commit()
    return Message(detail="recorded")


@router.post("/heartbeat", response_model=Message)
async def heartbeat(
    payload: HeartbeatIn,
    response: Response,
    session: DbSession,
    user: OptionalUserDep,
    wu_vid: str | None = Cookie(default=None),
) -> Message:
    """Still here. Refreshes the visitor's presence row and writes nothing else."""
    visitor_id = _visitor(response, wu_vid)
    if not _heartbeats.allow(visitor_id):
        return Message(detail="throttled")
    await telemetry.touch_presence(
        session,
        visitor_hash=hash_visitor(visitor_id),
        user_id=uuid.UUID(user.id) if user is not None else None,
        path=payload.path,
        is_staff=telemetry.is_staff_user(user),
    )
    await session.commit()
    return Message(detail="recorded")
