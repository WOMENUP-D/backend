"""What the portal keeps about a marketplace vacancy it does not own.

The listings themselves live on the board and are read live. The one thing
the portal adds — the courses it chose for a listing's requirements — is
kept here, keyed by the board and the board's id, with the mode, model and
prompt versions and trace id inside the payload (`services.vacancy_courses`).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class VacancyCourseSuggestion(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "vacancy_course_suggestions"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_vacancy_course_suggestions_listing"),
    )

    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
