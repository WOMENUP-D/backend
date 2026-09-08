"""Development plan schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.core.constants import GoalHorizon, PlanItemStatus, Priority, ScoreDimension
from app.schemas.common import ORMModel


class PlanItemRead(ORMModel):
    id: uuid.UUID
    order_index: int
    action: str
    description: str | None = None
    dimension: ScoreDimension | None = None
    priority: Priority
    status: PlanItemStatus
    due_date: date | None = None
    program_id: uuid.UUID | None = None
    opportunity_id: uuid.UUID | None = None


class PlanItemUpdate(BaseModel):
    status: PlanItemStatus | None = None
    due_date: date | None = None


class PlanRead(ORMModel):
    id: uuid.UUID
    horizon: GoalHorizon
    title: str
    summary: str | None = None
    is_active: bool
    accepted_at: datetime | None = None
    generated_by_ai: bool
    model_version: str | None = None
    prompt_version: str | None = None
    rationale: dict
    items: list[PlanItemRead]
    progress_percent: int
    created_at: datetime


class PlanGenerateRequest(BaseModel):
    horizon: GoalHorizon = GoalHorizon.M6
    focus_dimensions: list[ScoreDimension] = Field(
        default=[], max_length=3, description="Leave empty to let the AI choose."
    )
    # The locale she is reading the site in. Without it the plan follows a
    # stored column that nothing used to write, so every roadmap came back in
    # Uzbek even for a user on the Russian pages. Unrecognised values fall
    # through to the stored language rather than being rejected: a roadmap is
    # worth more than a 422 over a locale tag.
    language: str | None = Field(default=None, max_length=8, description="uz | uz-Cyrl | ru | en")


class PlanAccept(BaseModel):
    """Explicit user confirmation — an AI plan never activates on its own."""

    accepted: bool = True
    removed_item_ids: list[uuid.UUID] = []
