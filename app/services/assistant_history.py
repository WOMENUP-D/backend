"""Her saved assistant conversations: the rules behind the list.

The route module is the boundary; everything decided about a conversation is
decided here.

* **Found through its owner.** Every read, write and delete looks a
  conversation up by `(id, caller)` together. An id that belongs to somebody
  else is indistinguishable from one that does not exist, and it cannot be
  written over either: saving under another woman's id is the same 404.
* **Bounded.** A conversation holds at most `MAX_TURNS` turns and
  `MAX_BYTES` of JSON; an account keeps at most `MAX_CONVERSATIONS`, the
  oldest going first when a new one is started. The browser trims to the same
  limits, so in practice these only refuse a client that does not.
* **Turns are checked, not trusted.** Each must say who spoke and carry text;
  anything else it carries (the answer's cards, the Coach's references) is
  kept as the page drew it and never read by the server.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import AssistantConversation
from app.schemas.conversation import ConversationIn

MAX_CONVERSATIONS = 50
MAX_TURNS = 60
MAX_BYTES = 256_000
MAX_TEXT = 20_000


class ConversationError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _clean_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(turns) > MAX_TURNS:
        raise ConversationError("too_many_turns")
    for turn in turns:
        if turn.get("role") not in ("user", "assistant"):
            raise ConversationError("bad_turn")
        text = turn.get("text")
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ConversationError("bad_turn")
    if len(json.dumps(turns, ensure_ascii=False).encode()) > MAX_BYTES:
        raise ConversationError("too_large")
    return turns


async def list_for(session: AsyncSession, user_id: uuid.UUID) -> list[AssistantConversation]:
    result = await session.execute(
        select(AssistantConversation)
        .where(AssistantConversation.user_id == user_id)
        .order_by(AssistantConversation.updated_at.desc())
        .limit(MAX_CONVERSATIONS)
    )
    return list(result.scalars())


async def get_for(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> AssistantConversation | None:
    return await session.scalar(
        select(AssistantConversation).where(
            AssistantConversation.id == conversation_id,
            AssistantConversation.user_id == user_id,
        )
    )


async def save(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID, payload: ConversationIn
) -> AssistantConversation:
    """Create or replace one conversation. Raises `ConversationError` with
    `not_found` when the id is somebody else's."""
    turns = _clean_turns(payload.turns)
    existing = await session.get(AssistantConversation, conversation_id)
    now = datetime.now(UTC)
    if existing is not None:
        if existing.user_id != user_id:
            raise ConversationError("not_found")
        existing.tab = payload.tab
        existing.title = payload.title.strip()[:120]
        existing.turns = turns
        existing.updated_at = now
        await session.flush()
        return existing

    conversation = AssistantConversation(
        id=conversation_id,
        user_id=user_id,
        tab=payload.tab,
        title=payload.title.strip()[:120],
        turns=turns,
        created_at=now,
        updated_at=now,
    )
    session.add(conversation)
    await session.flush()
    await _keep_newest(session, user_id)
    return conversation


async def _keep_newest(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Over the cap, the conversations she has not touched longest go."""
    count = await session.scalar(
        select(func.count())
        .select_from(AssistantConversation)
        .where(AssistantConversation.user_id == user_id)
    )
    excess = int(count or 0) - MAX_CONVERSATIONS
    if excess <= 0:
        return
    oldest = (
        select(AssistantConversation.id)
        .where(AssistantConversation.user_id == user_id)
        .order_by(AssistantConversation.updated_at.asc())
        .limit(excess)
    )
    await session.execute(delete(AssistantConversation).where(AssistantConversation.id.in_(oldest)))


async def remove(session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID) -> bool:
    result = await session.execute(
        delete(AssistantConversation).where(
            AssistantConversation.id == conversation_id,
            AssistantConversation.user_id == user_id,
        )
    )
    return bool(result.rowcount)
