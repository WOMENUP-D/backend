"""The vacancy marketplace, independent of who supplies the vacancies.

WomanUP shows real job listings from an outside job board. Today that board
is HeadHunter (hh.uz); tomorrow it is meant to be Tijorat Markazi's labour
market. Everything the portal's pages know about a vacancy is the shape
defined here — a provider reads its own API and answers in these types, and
the API layer and the frontend never see a provider's raw fields.

Switching providers is configuration (`settings.vacancy_provider`), not a
rewrite: a new board implements `VacancyProvider` and registers its name in
`services.vacancies.get_provider`.

What a provider promises, and what it must not do:

* Everything it returns is the board's own data — the salary the employer
  wrote, the date the listing was published, the requirements as published.
  A provider never fills a gap with a guess; a field it does not have is
  `None`, and the page says "not specified".
* A listing the board has taken down answers as `None` (gone) or carries
  `is_active=False` (archived); it is never shown as open.
* Every listing keeps its original id and its link back to the board, where
  the woman applies. The portal takes no applications of its own for these.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

#: The ids the portal filters on. They happen to be HeadHunter's dictionary
#: values, chosen because they are also the plain vocabulary of a job search;
#: a future provider maps its own terms onto them.
EXPERIENCE = ("noExperience", "between1And3", "between3And6", "moreThan6")
EMPLOYMENT = ("full", "part", "project", "volunteer", "probation")
SCHEDULE = ("fullDay", "shift", "flexible", "remote", "flyInFlyOut")
WORK_FORMAT = ("ON_SITE", "REMOTE", "HYBRID", "FIELD_WORK")
SORT = ("relevance", "recent", "salary")
#: WomanUP's own shortcuts — ways in for a student, a first job, a mother who
#: needs the hours to fit. Each is a preset over the filters above, so a
#: provider does not have to know them; see `VacancyQuery.resolve_picks`.
PICKS = ("no_experience", "internship", "remote", "part_time", "junior")

Sort = Literal["relevance", "recent", "salary"]


class ProviderError(RuntimeError):
    """The board could not answer."""


class ProviderUnconfigured(ProviderError):
    """The board needs credentials the operator has not supplied."""


class ProviderUnavailable(ProviderError):
    """The board is unreachable or answered with an error."""


@dataclass(frozen=True, slots=True)
class Salary:
    """Exactly what the employer wrote. Both bounds may be missing, in which
    case the listing states no salary and the page says so."""

    amount_from: int | None
    amount_to: int | None
    currency: str | None
    #: Before tax, when the board says which. None when it does not.
    gross: bool | None = None


@dataclass(frozen=True, slots=True)
class Employer:
    id: str | None
    name: str
    #: The employer's page on the board.
    url: str | None = None
    logo_url: str | None = None
    #: Filled on a detail read, when the board publishes it.
    description_html: str | None = None
    industries: tuple[str, ...] = ()
    site_url: str | None = None
    #: The board's own mark that the employer is verified, when it has one.
    trusted: bool | None = None


@dataclass(frozen=True, slots=True)
class VacancySummary:
    """A listing as the catalogue shows it."""

    provider: str
    id: str
    title: str
    employer: Employer
    salary: Salary | None
    #: The place as the board names it ("Ташкент").
    area: str | None
    #: The platform's region key for that place, when it is known.
    region: str | None
    work_formats: tuple[str, ...]
    schedule: str | None
    employment: str | None
    experience: str | None
    published_at: datetime | None
    #: The board's own short extract, as plain text.
    snippet: str
    #: The original listing, where she applies.
    url: str
    #: The language the listing is written in: `uz` or `ru`.
    language: str
    is_internship: bool
    professional_roles: tuple[str, ...] = ()
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class VacancyDetail(VacancySummary):
    """Everything the board publishes about one listing."""

    #: The employer's full text, sanitised to a small allowlist of tags.
    description_html: str = ""
    description_text: str = ""
    #: The board's structured key skills, as written.
    skills: tuple[str, ...] = ()
    #: Language requirements, as "name — level".
    languages: tuple[str, ...] = ()
    address: str | None = None
    #: The board asks applicants to take a test before applying.
    has_test: bool = False


@dataclass(frozen=True, slots=True)
class VacancyPage:
    items: tuple[VacancySummary, ...]
    total: int
    page: int
    pages: int
    per_page: int


@dataclass(frozen=True, slots=True)
class RoleGroup:
    """A professional category the board groups its roles into."""

    id: str
    name: str
    role_ids: tuple[str, ...]


@dataclass(slots=True)
class VacancyQuery:
    """What she asked for. Validated at the API edge; a provider may assume
    every value here is one of the constants above."""

    text: str | None = None
    #: The platform's region key, or None for the whole country.
    region: str | None = None
    salary_from: int | None = None
    only_with_salary: bool = False
    experience: str | None = None
    employment: str | None = None
    schedule: str | None = None
    work_format: str | None = None
    professional_roles: list[str] = field(default_factory=list)
    sort: Sort = "relevance"
    picks: list[str] = field(default_factory=list)
    page: int = 1
    per_page: int = 20

    def resolve_picks(self) -> VacancyQuery:
        """The query with WomanUP's shortcuts folded into plain filters.

        A pick never overrides a filter she set by hand: "no experience" on
        top of an explicit experience choice leaves her choice standing.
        """
        out = VacancyQuery(**{f: getattr(self, f) for f in self.__slots__ if f != "picks"})
        out.professional_roles = list(self.professional_roles)
        extra_text: list[str] = []
        for pick in self.picks:
            if pick == "no_experience" and out.experience is None:
                out.experience = "noExperience"
            elif pick == "internship" and out.employment is None:
                out.employment = "probation"
            elif pick == "remote" and out.schedule is None and out.work_format is None:
                out.schedule = "remote"
            elif pick == "part_time" and out.employment is None:
                out.employment = "part"
            elif pick == "junior":
                extra_text.append("(junior OR младший OR помощник OR ассистент OR стажер)")
        if extra_text:
            base = f"({out.text}) AND " if out.text else ""
            out.text = base + " AND ".join(extra_text)
        return out


class VacancyProvider(ABC):
    """One job board, read through its own API."""

    #: The configuration name (`settings.vacancy_provider`).
    name: str
    #: How the page names the source ("HeadHunter").
    label: str
    #: The board's site, for the source line.
    site_url: str

    @abstractmethod
    async def search(self, query: VacancyQuery) -> VacancyPage: ...

    @abstractmethod
    async def get(self, vacancy_id: str) -> VacancyDetail | None:
        """One listing in full, or None when the board no longer has it."""

    @abstractmethod
    async def role_groups(self) -> tuple[RoleGroup, ...]:
        """The board's professional categories, for the category filter."""

    @abstractmethod
    async def regions(self) -> tuple[str, ...]:
        """The platform region keys this board can filter on."""
