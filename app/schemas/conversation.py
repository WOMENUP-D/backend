"""The assistant conversation list: what the browser sends and reads back."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Tab = Literal["coach", "auto", "education", "health", "portal"]


class ConversationSummary(BaseModel):
    """A row in the list: enough to draw it, none of the turns."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tab: Tab
    title: str
    updated_at: datetime
    turn_count: int


class ConversationRead(BaseModel):
    id: uuid.UUID
    tab: Tab
    title: str
    updated_at: datetime
    turns: list[dict[str, Any]]


class ConversationIn(BaseModel):
    """The whole conversation, as the page holds it. Sent again after each
    answer; the id is the browser's, so a retry is the same conversation."""

    tab: Tab
    title: str = Field(min_length=1, max_length=120)
    turns: list[dict[str, Any]] = Field(min_length=1)
