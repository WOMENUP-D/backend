"""The marketplace API over a boxed provider: her filters reach the
provider as she set them, listings come back in the portal's shape, a gone
listing is 404, a down board is 503, and the courses for a listing are kept."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.api import vacancies as api
from app.core.constants import Language, ProgramCategory, ProgramFormat
from app.models.program import Program
from app.models.vacancy import VacancyCourseSuggestion
from app.services.vacancies.base import (
    Employer,
    ProviderUnavailable,
    RoleGroup,
    Salary,
    VacancyDetail,
    VacancyPage,
    VacancyProvider,
    VacancyQuery,
    VacancySummary,
)

BASE = "/api/v1/vacancies"


def _summary(vid: str, title: str, **overrides) -> VacancySummary:
    fields = {
        "provider": "fake",
        "id": vid,
        "title": title,
        "employer": Employer(id="1", name="Alfa Savdo", url="https://board.test/employer/1"),
        "salary": Salary(6000000, None, "UZS", False),
        "area": "Ташкент",
        "region": "tashkent_city",
        "work_formats": ("ON_SITE",),
        "schedule": "fullDay",
        "employment": "full",
        "experience": "between1And3",
        "published_at": datetime(2026, 10, 30, 5, 0, tzinfo=UTC),
        "snippet": "Ведение учёта",
        "url": f"https://board.test/vacancy/{vid}",
        "language": "ru",
        "is_internship": False,
    }
    fields.update(overrides)
    return VacancySummary(**fields)


def _detail(vid: str, title: str, **overrides) -> VacancyDetail:
    base = _summary(
        vid, title, **{k: v for k, v in overrides.items() if k in VacancySummary.__slots__}
    )
    extra = {k: v for k, v in overrides.items() if k not in VacancySummary.__slots__}
    return VacancyDetail(
        **{f: getattr(base, f) for f in VacancySummary.__slots__},
        description_html="<p>Ведение учёта в 1С и Excel.</p>",
        description_text="Ведение учёта в 1С и Excel.",
        skills=("Excel", "1С: Бухгалтерия"),
        **extra,
    )


class FakeBoard(VacancyProvider):
    name = "fake"
    label = "Fake Board"
    site_url = "https://board.test"

    def __init__(self) -> None:
        self.queries: list[VacancyQuery] = []
        self.listings = {
            "1": _detail("1", "Бухгалтер"),
            "2": _detail("2", "Кассир", is_active=False, salary=None),
        }
        self.down = False

    async def search(self, query: VacancyQuery) -> VacancyPage:
        if self.down:
            raise ProviderUnavailable("board is down")
        self.queries.append(query)
        return VacancyPage(
            items=tuple(self.listings.values()),
            total=2,
            page=query.page,
            pages=1,
            per_page=query.per_page,
        )

    async def get(self, vacancy_id: str) -> VacancyDetail | None:
        if self.down:
            raise ProviderUnavailable("board is down")
        return self.listings.get(vacancy_id)

    async def role_groups(self) -> tuple[RoleGroup, ...]:
        return (RoleGroup("3", "Финансы, бухгалтерия", ("18", "134")),)

    async def regions(self) -> tuple[str, ...]:
        return ("tashkent_city", "samarkand")


@pytest.fixture
def board(monkeypatch) -> FakeBoard:
    fake = FakeBoard()
    monkeypatch.setattr(api, "get_provider", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_meta_names_the_board_and_what_can_be_filtered(app_client, board):
    body = (await app_client.get(f"{BASE}/meta")).json()
    assert body["provider"] == {
        "name": "fake",
        "label": "Fake Board",
        "site_url": "https://board.test",
    }
    assert body["regions"] == ["tashkent_city", "samarkand"]
    assert body["experience"][0] == "noExperience"
    assert body["picks"] == ["no_experience", "internship", "remote", "part_time", "junior"]
    assert body["categories"] == [{"id": "3", "name": "Финансы, бухгалтерия", "roles": 2}]


@pytest.mark.asyncio
async def test_her_filters_reach_the_board_as_she_set_them(app_client, board):
    response = await app_client.get(
        f"{BASE}?q=бухгалтер&region=tashkent_city&salary_from=5000000&only_with_salary=true"
        "&experience=noExperience&employment=part&category=3&sort=salary&pick=remote&pick=junior"
        "&page=2&per_page=10"
    )
    assert response.status_code == 200
    query = board.queries[-1]
    assert query.text == "бухгалтер" and query.region == "tashkent_city"
    assert query.salary_from == 5000000 and query.only_with_salary is True
    assert query.experience == "noExperience" and query.employment == "part"
    assert query.professional_roles == ["18", "134"]
    assert query.sort == "salary" and query.picks == ["remote", "junior"]
    assert query.page == 2 and query.per_page == 10

    body = response.json()
    assert body["total"] == 2 and body["page"] == 2
    first, second = body["items"]
    assert first["title"] == "Бухгалтер"
    assert first["salary"] == {
        "amount_from": 6000000,
        "amount_to": None,
        "currency": "UZS",
        "gross": False,
    }
    assert first["employer"]["name"] == "Alfa Savdo"
    assert first["url"] == "https://board.test/vacancy/1"
    assert first["published_at"].startswith("2026-10-30T05:00:00")
    # The board stated no salary: the page gets null, never a guess.
    assert second["salary"] is None and second["is_active"] is False


@pytest.mark.asyncio
async def test_a_value_outside_the_vocabulary_is_refused(app_client, board):
    assert (await app_client.get(f"{BASE}?experience=senior")).status_code == 422
    assert (await app_client.get(f"{BASE}?category=99")).status_code == 422
    assert (await app_client.get(f"{BASE}?pick=cheap")).status_code == 422
    assert board.queries == []


@pytest.mark.asyncio
async def test_one_listing_in_full_and_a_gone_one_as_404(app_client, board):
    body = (await app_client.get(f"{BASE}/1")).json()
    assert body["description_html"] == "<p>Ведение учёта в 1С и Excel.</p>"
    assert body["skills"] == ["Excel", "1С: Бухгалтерия"]
    assert body["is_active"] is True
    assert (await app_client.get(f"{BASE}/2")).json()["is_active"] is False
    assert (await app_client.get(f"{BASE}/404")).status_code == 404
    assert (await app_client.get(f"{BASE}/not%20an%20id!")).status_code == 422


@pytest.mark.asyncio
async def test_a_board_that_is_down_is_a_503_with_a_reason(app_client, board):
    board.down = True
    response = await app_client.get(BASE)
    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "provider_unavailable"
    assert (await app_client.get(f"{BASE}/1")).status_code == 503


@pytest.mark.asyncio
async def test_the_courses_for_a_listing_are_chosen_once_and_kept(client, session, board):
    session.add(
        Program(
            slug="excel-functions",
            title_i18n={"ru": "Полезные функции Excel"},
            goal_i18n={},
            description_i18n={},
            category=ProgramCategory.VOCATIONAL_SKILLS,
            format=ProgramFormat.VIDEO,
            language=Language.RU,
            skills_taught=["excel"],
            is_published=True,
            published_at=datetime.now(UTC),
            provider="Stepik",
            external_url="https://stepik.org/course/1",
        )
    )
    await session.flush()

    body = (await client.get(f"{BASE}/1/courses")).json()
    assert body["provider"] == "fake" and body["vacancy_id"] == "1"
    assert body["mode"] == "rules"
    assert [item["program"]["slug"] for item in body["items"]] == ["excel-functions"]
    assert body["items"][0]["covers"] == ["Excel"]

    again = (await client.get(f"{BASE}/1/courses")).json()
    assert again["generated_at"] == body["generated_at"]
    rows = list((await session.execute(select(VacancyCourseSuggestion))).scalars())
    assert [(row.provider, row.external_id) for row in rows] == [("fake", "1")]
    assert rows[0].payload["mode"] == "rules"

    assert (await client.get(f"{BASE}/404/courses")).status_code == 404
