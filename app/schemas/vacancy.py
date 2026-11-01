"""The vacancy marketplace on the wire — provider-independent.

These mirror `services.vacancies.base`: whatever board supplies the listings,
the page receives this shape. Nothing here is computed about the reader.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.opportunity import SuggestedCourse


class SalaryRead(BaseModel):
    """What the employer wrote, and nothing else. Both bounds absent means
    the page says "not specified"."""

    amount_from: int | None = None
    amount_to: int | None = None
    currency: str | None = None
    gross: bool | None = None


class EmployerRead(BaseModel):
    id: str | None = None
    name: str
    url: str | None = None
    logo_url: str | None = None
    description_html: str | None = None
    industries: list[str] = []
    site_url: str | None = None
    trusted: bool | None = None


class VacancyRead(BaseModel):
    provider: str
    id: str
    title: str
    employer: EmployerRead
    salary: SalaryRead | None = None
    area: str | None = None
    region: str | None = None
    work_formats: list[str] = []
    schedule: str | None = None
    employment: str | None = None
    experience: str | None = None
    published_at: datetime | None = None
    snippet: str = ""
    url: str
    language: str = "ru"
    is_internship: bool = False
    professional_roles: list[str] = []
    is_active: bool = True


class VacancyDetailRead(VacancyRead):
    description_html: str = ""
    description_text: str = ""
    skills: list[str] = []
    languages: list[str] = []
    address: str | None = None
    has_test: bool = False


class VacancyPageRead(BaseModel):
    items: list[VacancyRead]
    total: int
    page: int
    pages: int
    per_page: int


class ProviderInfo(BaseModel):
    name: str
    label: str
    site_url: str


class RoleGroupRead(BaseModel):
    id: str
    name: str
    roles: int = 0


class VacancyMetaRead(BaseModel):
    """What the filters can offer, so the page builds them from the server
    rather than from a copy of the board's dictionaries."""

    provider: ProviderInfo
    regions: list[str] = []
    experience: list[str] = []
    employment: list[str] = []
    schedule: list[str] = []
    work_format: list[str] = []
    sort: list[str] = []
    picks: list[str] = []
    categories: list[RoleGroupRead] = []


class VacancyCoursesRead(BaseModel):
    """The portal's courses for one listing's requirements; see
    `services.vacancy_courses`."""

    provider: str
    vacancy_id: str
    mode: str = Field(pattern="^(ai|rules)$")
    generated_at: datetime | None = None
    items: list[SuggestedCourse] = []
