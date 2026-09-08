"""Shared response envelopes, query primitives and cross-schema validators."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import MAX_SUPPORTED_AGE, MIN_SUPPORTED_AGE, years_between


def validate_birth_date(value: date | None) -> date | None:
    """A date of birth that could belong to a user of this portal.

    Lives here because two unrelated schemas need the identical rule — the
    sign-up payload and the profile update — and a second copy would be the one
    that drifts. Deliberately strict at the bottom and loose at the top: the
    lower bound is the safety boundary the age gate exists for, the upper one
    only catches a slipped digit in a year.

    Attach it to INPUT models only. It reads the wall clock, so a stored date
    that ages past the ceiling would turn a response model into a 500 on read
    rather than the write into a 422.
    """
    if value is None:
        return None
    today = date.today()
    if value > today:
        raise ValueError("birth date cannot be in the future")
    age = years_between(value, today)
    if age < MIN_SUPPORTED_AGE:
        raise ValueError(f"the portal is for users aged {MIN_SUPPORTED_AGE} and over")
    if age > MAX_SUPPORTED_AGE:
        raise ValueError("birth date is implausible")
    return value


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
