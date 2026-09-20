"""Rate limits that hold across workers, restarts and failed requests.

Counted in PostgreSQL. The database is already required to sign in, so the
login path gains no new dependency it can fail on; Redis is in the stack but
nothing else uses it yet, and an auth limiter that stops working when Redis is
unreachable would either lock everybody out or quietly let everybody through.

Two rules make these counters trustworthy:

* **They are committed immediately.** A rejected sign-in ends in an exception,
  and the request's transaction is rolled back with it — which is exactly how
  the OTP attempt counter came to mean nothing. A limiter that forgets the
  attempt it just counted is not a limiter.
* **They never store what they count.** The bucket is an HMAC of the scope,
  the key and the window, so an e-mail address or an IP cannot be read back
  out of the table.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.rate_limit import AuthThrottle


@dataclass(frozen=True, slots=True)
class Limit:
    """How many events of one kind a single key may cause in one window."""

    scope: str
    limit: int
    window_seconds: int


def _bucket(scope: str, key: str, window_start: int) -> str:
    message = f"{scope}:{key.strip().lower()}:{window_start}".encode()
    return hmac.new(settings.jwt_secret_key.encode(), message, sha256).hexdigest()


async def take(session: AsyncSession, limit: Limit, key: str) -> bool:
    """Count one event against `key`. False once the window's allowance is gone.

    The write is committed on its own, so the caller may raise afterwards and
    still have the attempt counted. Call it before the request has other work
    pending: the commit ends the request's transaction as it stands.
    """
    now = datetime.now(UTC)
    window_start = int(now.timestamp()) // limit.window_seconds * limit.window_seconds
    expires_at = datetime.fromtimestamp(window_start, UTC) + timedelta(seconds=limit.window_seconds)
    bucket = _bucket(limit.scope, key, window_start)

    statement = (
        insert(AuthThrottle)
        .values(bucket=bucket, hits=1, expires_at=expires_at)
        .on_conflict_do_update(
            index_elements=[AuthThrottle.bucket],
            set_={"hits": AuthThrottle.__table__.c.hits + 1},
        )
        .returning(AuthThrottle.hits)
    )
    hits = int(await session.scalar(statement) or 1)
    if hits == 1:
        # A new window: a good moment to drop the windows that have passed.
        await session.execute(delete(AuthThrottle).where(AuthThrottle.expires_at < now))
    await session.commit()
    return hits <= limit.limit


def otp_request_limits() -> tuple[Limit, Limit]:
    """Per IP and per address, for asking for a code."""
    return (
        Limit(
            "otp_request_ip",
            settings.otp_request_ip_limit,
            settings.otp_request_ip_window_seconds,
        ),
        Limit(
            "otp_request_identifier",
            settings.otp_request_identifier_limit,
            settings.otp_request_identifier_window_seconds,
        ),
    )


def otp_verify_limits() -> tuple[Limit, Limit]:
    """Per IP and per address, for trying a code."""
    return (
        Limit(
            "otp_verify_ip",
            settings.otp_verify_ip_limit,
            settings.otp_verify_ip_window_seconds,
        ),
        Limit(
            "otp_verify_identifier",
            settings.otp_verify_identifier_limit,
            settings.otp_verify_identifier_window_seconds,
        ),
    )
