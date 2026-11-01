"""How often the model may be asked, per account and per anonymous address.

Every assistant answer costs money and the assistant is open to visitors, so
a script that asked without limit could spend the portal's budget — and a
guest allowance keyed only on a header the client chooses was no limit at all.
These counters live in the database (`services.rate_limit`), so they hold
across workers and restarts, store no address or id in the clear, and are
committed before the model is called.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip
from app.core import security_log
from app.core.config import settings
from app.services import rate_limit

HOUR = 3600
LIMIT_REACHED = "ai_limit_reached"


def user_limit() -> rate_limit.Limit:
    return rate_limit.Limit("ai_user", settings.ai_user_hourly_limit, HOUR)


def guest_limit() -> rate_limit.Limit:
    return rate_limit.Limit("ai_guest_ip", settings.ai_guest_ip_hourly_limit, HOUR)


async def take(session: AsyncSession, request: Request, user_id: uuid.UUID | None) -> None:
    """Spend one model-backed answer, or refuse with 429.

    Signed in, the account is the key; anonymous, the connecting address is.
    Call it before the request has other work pending: the counter commits.
    """
    if user_id is not None:
        allowed = await rate_limit.take(session, user_limit(), str(user_id))
    else:
        allowed = await rate_limit.take(session, guest_limit(), client_ip(request) or "unknown")
    if not allowed:
        security_log.event(
            "ai.limit_reached",
            user_id=str(user_id) if user_id else None,
            ip=None if user_id else client_ip(request),
        )
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=LIMIT_REACHED)
