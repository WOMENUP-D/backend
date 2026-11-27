"""Her saved assistant conversations.

Thin by design — every rule lives in `services.assistant_history`. Every route
answers for the caller in the token; there is no route that takes a user id,
so a request for somebody else's conversations cannot be expressed. An id
that is not hers is a 404, the same as an id that does not exist.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response, status

from app.api.deps import CurrentUserDep, DbSession
from app.schemas.conversation import ConversationIn, ConversationRead, ConversationSummary
from app.services import assistant_history

router = APIRouter(prefix="/assistant/conversations", tags=["assistant"])

_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "too_large": status.HTTP_413_CONTENT_TOO_LARGE,
    "too_many_turns": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "bad_turn": status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _read(conversation) -> ConversationRead:
    return ConversationRead(
        id=conversation.id,
        tab=conversation.tab,
        title=conversation.title,
        updated_at=conversation.updated_at,
        turns=conversation.turns,
    )


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(user: CurrentUserDep, session: DbSession) -> list[ConversationSummary]:
    """Her conversations, newest first — titles only, the turns on demand."""
    rows = await assistant_history.list_for(session, uuid.UUID(user.id))
    return [
        ConversationSummary(
            id=row.id,
            tab=row.tab,
            title=row.title,
            updated_at=row.updated_at,
            turn_count=len(row.turns),
        )
        for row in rows
    ]


@router.get("/{conversation_id}", response_model=ConversationRead)
async def read_conversation(
    conversation_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> ConversationRead:
    row = await assistant_history.get_for(session, uuid.UUID(user.id), conversation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return _read(row)


@router.put("/{conversation_id}", response_model=ConversationRead)
async def save_conversation(
    conversation_id: uuid.UUID, payload: ConversationIn, user: CurrentUserDep, session: DbSession
) -> ConversationRead:
    """Create or replace the conversation. Sent again after every answer."""
    try:
        row = await assistant_history.save(session, uuid.UUID(user.id), conversation_id, payload)
    except assistant_history.ConversationError as exc:
        raise HTTPException(status_code=_STATUS[exc.reason], detail=exc.reason) from exc
    return _read(row)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID, user: CurrentUserDep, session: DbSession
) -> Response:
    removed = await assistant_history.remove(session, uuid.UUID(user.id), conversation_id)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
