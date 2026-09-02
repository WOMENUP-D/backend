"""AI navigator request/response contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.constants import RiskFlagType
from app.schemas.common import ORMModel


class NavigatorQuery(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None
    language: str | None = None


class SourceRef(BaseModel):
    """A retrieved, approved knowledge chunk backing part of an answer."""

    document_id: uuid.UUID
    document_title: str
    chunk_id: uuid.UUID
    score: float
    excerpt: str


class NavigatorAnswer(BaseModel):
    answer: str
    sources: list[SourceRef] = []
    confidence: float = Field(ge=0, le=1)
    # True when confidence is below threshold: the assistant says it does not
    # know rather than guessing.
    is_uncertain: bool = False
    # True when the topic requires a licensed human (medical, legal, safety).
    escalated_to_human: bool = False
    escalation_reason: str | None = None
    suggested_actions: list[str] = []
    trace_id: str
    conversation_id: uuid.UUID | None = None


class NextStepCard(BaseModel):
    """The "Bugungi qadam" card on the cabinet home screen."""

    title: str
    description: str
    action_url: str | None = None
    dimension: str | None = None


class RiskFlagRead(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    flag_type: RiskFlagType
    severity: str
    reason: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class KnowledgeDocumentCreate(BaseModel):
    title: str = Field(max_length=300)
    source_type: str = Field(pattern="^(program|guideline|faq|legal|classifier|partner_catalog)$")
    content: str = Field(min_length=1)
    source_url: str | None = None
    language: str = "uz"
    tags: list[str] = []


class KnowledgeDocumentRead(ORMModel):
    id: uuid.UUID
    title: str
    source_type: str
    language: str
    tags: list[str]
    is_approved: bool
    version: int
    created_at: datetime
