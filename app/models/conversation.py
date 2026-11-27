"""Her conversations with the assistant, kept so she can come back to them.

Until this table the browser held the conversation and the server kept only
the trace row (`ai_interactions`: model, prompt version, counts and, for a
while, excerpts). That kept nothing personal on the server, but it also meant
yesterday's conversation was gone the moment she changed phone. This table is
her own copy, the way a messenger keeps a chat:

* **Hers alone.** Read, written and deleted only through the caller's own
  token; there is no route that takes another user's id, and no staff view.
* **What she saw.** The turns are stored as the page drew them — her question,
  the answer and the cards under it — so reopening a chat shows the same
  thing. They are never fed back to the model from here: a follow-up still
  sends its own bounded, scrubbed `history` (services/conversation.py).
* **Bounded and temporary.** A size cap per conversation and a count cap per
  account (services/assistant_history.py); conversations untouched for
  `assistant_chat_retention_days` are removed by the retention sweep, and all
  of them go with the account on erasure.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class AssistantConversation(UUIDMixin, TimestampMixin, Base):
    """One conversation: its title, the capability it was asked in, its turns."""

    __tablename__ = "assistant_conversations"
    __table_args__ = (
        # The list is always "hers, newest first".
        Index("ix_assistant_conversations_user_updated", "user_id", "updated_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: coach | auto | education | health | portal — the tab it was asked in.
    tab: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Her first question, on one line.
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    #: The turns as the page drew them; see the module docstring.
    turns: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
