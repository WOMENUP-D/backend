"""Shared response envelopes and query primitives."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    """Base for schemas read straight off a SQLAlchemy row."""

    model_config = ConfigDict(from_attributes=True)


class PaginationParams(BaseModel):
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


class Page[T](BaseModel):
    items: list[T]
    total: int
    page: int
    size: int

    @property
    def pages(self) -> int:
        return (self.total + self.size - 1) // self.size if self.size else 0


class Message(BaseModel):
    detail: str


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None
    request_id: str | None = None
