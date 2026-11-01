"""Keeping data only as long as it is useful.

Three kinds of rows outlive their purpose if nothing removes them: the
assistant's prompt and answer excerpts (personal text kept for tracing a
recommendation, which nobody needs to read months later), spent or expired
one-time codes, and the bookkeeping rows behind rate limits and token
revocation once their windows have passed. The worker runs this once an
hour; `erase_user` runs it for one account the moment she asks for erasure.

What is never touched: the trace itself. An `ai_interactions` row keeps its
model version, prompt version, trace id and counts — section 06 requires
that — only the excerpts are blanked.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AiInteraction
from app.models.auth_token import RevokedToken
from app.models.rate_limit import AuthThrottle
from app.models.user import OtpChallenge


@dataclass(slots=True)
class Purged:
    ai_excerpts: int = 0
    otp_challenges: int = 0
    revoked_tokens: int = 0
    throttle_rows: int = 0


async def purge(session: AsyncSession, *, now: datetime | None = None) -> Purged:
    """One sweep. Flushes nothing on its own; the caller commits."""
    now = now or datetime.now(UTC)
    out = Purged()

    excerpt_cutoff = now - timedelta(days=max(1, settings.ai_trace_retention_days))
    result = await session.execute(
        update(AiInteraction)
        .where(
            AiInteraction.created_at < excerpt_cutoff,
            (AiInteraction.prompt_excerpt.is_not(None))
            | (AiInteraction.response_excerpt.is_not(None)),
        )
        .values(prompt_excerpt=None, response_excerpt=None)
    )
    out.ai_excerpts = result.rowcount or 0

    otp_cutoff = now - timedelta(hours=max(1, settings.otp_retention_hours))
    result = await session.execute(delete(OtpChallenge).where(OtpChallenge.created_at < otp_cutoff))
    out.otp_challenges = result.rowcount or 0

    result = await session.execute(delete(RevokedToken).where(RevokedToken.expires_at < now))
    out.revoked_tokens = result.rowcount or 0

    result = await session.execute(delete(AuthThrottle).where(AuthThrottle.expires_at < now))
    out.throttle_rows = result.rowcount or 0
    return out


async def erase_user(
    session: AsyncSession, user_id: uuid.UUID, identifiers: list[str] | None = None
) -> None:
    """What erasure removes beyond the account row: the text of her
    conversations with the assistant, and any one-time codes addressed to
    her. The traces stay, blank; the aggregate figures stay, anonymous."""
    await session.execute(
        update(AiInteraction)
        .where(AiInteraction.user_id == user_id)
        .values(prompt_excerpt=None, response_excerpt=None)
    )
    clean = [value for value in (identifiers or []) if value]
    if clean:
        await session.execute(delete(OtpChallenge).where(OtpChallenge.identifier.in_(clean)))
