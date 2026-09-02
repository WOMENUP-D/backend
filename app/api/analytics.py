"""Traffic collection.

Open to anonymous callers by necessity: the traffic worth counting is mostly
people who have not signed up yet. That makes the write path the exposed one,
so it accepts a route and nothing else, stores no IP or user agent, and caps
how often one visitor can add rows.
"""

from __future__ import annotations

import secrets
import time
import uuid

from fastapi import APIRouter, Cookie, Response
from pydantic import BaseModel, Field

from app.api.deps import DbSession, OptionalUserDep
from app.schemas.common import Message
from app.services import traffic

router = APIRouter(prefix="/analytics", tags=["analytics"])

VISITOR_COOKIE = "wu_vid"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# One visitor may register a view at most this often. Navigation is bursty and
# a person legitimately opens several screens a minute, so the limit only has
# to stop a loop hammering the table.
MIN_SECONDS_BETWEEN_VIEWS = 1.0

# visitor id -> last accepted timestamp. In-process, like the assistant's guest
# quota: it resets on restart and does not span workers. Good enough to stop a
# runaway client; move to Redis when the portal runs more than one worker.
_last_seen: dict[str, float] = {}
_MAX_TRACKED = 20_000


class ViewIn(BaseModel):
    path: str = Field(max_length=200)
    locale: str | None = Field(default=None, max_length=10)


def _throttled(visitor_id: str) -> bool:
    now = time.monotonic()
    previous = _last_seen.get(visitor_id)
    if previous is not None and now - previous < MIN_SECONDS_BETWEEN_VIEWS:
        return True
    if len(_last_seen) > _MAX_TRACKED:
        _last_seen.clear()
    _last_seen[visitor_id] = now
    return False


@router.post("/view", response_model=Message)
async def record_view(
    payload: ViewIn,
    response: Response,
    session: DbSession,
    user: OptionalUserDep,
    wu_vid: str | None = Cookie(default=None),
) -> Message:
    """Record one screen opening. Fire-and-forget from the client's side."""
    visitor_id = wu_vid or secrets.token_urlsafe(16)

    if wu_vid != visitor_id:
        # First visit: hand out an id. First-party, no third party ever sees it,
        # and only its HMAC is stored.
        response.set_cookie(
            VISITOR_COOKIE,
            visitor_id,
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/",
        )

    if _throttled(visitor_id):
        return Message(detail="throttled")

    user_id: uuid.UUID | None = user.id if user is not None else None
    await traffic.record_view(
        session,
        visitor_id=visitor_id,
        path=payload.path,
        user_id=user_id,
        locale=payload.locale,
    )
    await session.commit()
    return Message(detail="recorded")
