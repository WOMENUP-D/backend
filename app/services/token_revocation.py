"""Remembering which tokens are no longer good.

Signing out revokes the session's tokens; refreshing spends the refresh token
it was given, so each one works exactly once. Both are rows in
`revoked_tokens`, looked up by the token id on every authenticated request.
The lookup is a unique-index hit — cheap, and the only thing standing between
"signed out" and "still signed in for half an hour".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth_token import RevokedToken


def _expiry(claims: dict[str, Any]) -> datetime:
    value = claims.get("exp")
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, UTC)
    return datetime.now(UTC)


async def revoke(session: AsyncSession, claims: dict[str, Any], *, reason: str) -> bool:
    """Refuse this token from now on.

    Returns whether *this* call revoked it. Two requests spending the same
    refresh token at the same moment both find it unrevoked; the unique index
    on `jti` lets exactly one of them insert the row, and that one is the only
    one that may be answered with new tokens. A token without an id cannot be
    revoked and reports False.
    """
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti:
        return False
    subject = claims.get("sub")
    try:
        user_id = uuid.UUID(str(subject)) if subject else None
    except ValueError:
        user_id = None
    inserted = await session.scalar(
        insert(RevokedToken)
        .values(jti=jti[:64], user_id=user_id, reason=reason[:20], expires_at=_expiry(claims))
        .on_conflict_do_nothing(index_elements=[RevokedToken.jti])
        .returning(RevokedToken.id)
    )
    return inserted is not None


async def revoke_all(session: AsyncSession, user_id: uuid.UUID, *, reason: str) -> None:
    """End every session of one account.

    Bumps the account's session generation; every token issued under the
    previous one is refused on its next request. `reason` is kept for the
    security log by the caller — nothing per token needs storing.
    """
    from app.models.user import User

    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(session_generation=User.session_generation + 1)
    )


def generation_matches(claims: dict[str, Any], current: int | None) -> bool:
    """Whether a token was issued under the account's current generation. A
    token without the claim predates generations and reads as 0."""
    if current is None:
        return True
    issued = claims.get("gen", 0)
    return isinstance(issued, int | float) and int(issued) == int(current)


async def is_revoked(session: AsyncSession, claims: dict[str, Any]) -> bool:
    """Whether this very token has been refused — signed out or spent."""
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti:
        # A token without an id predates revocation; it cannot be revoked,
        # and it expires like any other.
        return False
    return (
        await session.scalar(select(RevokedToken.id).where(RevokedToken.jti == jti[:64]))
    ) is not None


async def forget_expired(session: AsyncSession) -> None:
    """Drop rows for tokens that have expired on their own."""
    await session.execute(delete(RevokedToken).where(RevokedToken.expires_at < datetime.now(UTC)))
