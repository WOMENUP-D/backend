"""HeadHunter as a vacancy provider: faithful to the board, careful with
the API, and cached.

Pinned here, in order of harm: a listing is read exactly as published (the
employer's salary, the structured fields, the publication time, an Uzbek
title in the reformed alphabet); the employer's HTML is reduced to a small
allowlist; the filters she chose become the board's parameters and nothing
else; a refused call is retried once with a fresh token; an identical query
is answered from cache; a deleted listing is None and an archived one is
closed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.core.config import settings
from app.services import hh_jobs
from app.services.hh_jobs import HhClient
from app.services.vacancies.base import (
    ProviderUnconfigured,
    VacancyQuery,
)
from app.services.vacancies.cache import JsonCache
from app.services.vacancies.headhunter import (
    HeadHunterProvider,
    detail_from,
    parse_when,
    salary_from,
    summary_from,
)
from app.services.vacancies.text import sanitize_html
from tests.services.test_hh_jobs import UA, FakeHh, _detail, _item

AREAS = {
    "id": "97",
    "name": "Узбекистан",
    "areas": [
        {"id": "2759", "name": "Ташкент", "areas": []},
        {"id": "2761", "name": "Чирчик", "areas": []},
        {"id": "2764", "name": "Самарканд", "areas": []},
        {"id": "2766", "name": "Акташ (Узбекистан)", "areas": []},
    ],
}
ROLES = {
    "categories": [
        {"id": "3", "name": "Финансы, бухгалтерия", "roles": [{"id": "18"}, {"id": "134"}]},
        {"id": "11", "name": "Информационные технологии", "roles": [{"id": "96"}]},
    ]
}


class Board(FakeHh):
    """The feed test's boxed API, with the marketplace's extra endpoints and
    a record of every search's parameters."""

    def __init__(self) -> None:
        super().__init__()
        self.searches: list[dict[str, list[str]]] = []
        self.employers: dict[str, dict] = {}
        self.accepted_token = "app-token"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "app-token", "expires_in": 1209599})
        auth = request.headers.get("Authorization", "")
        if request.url.path in ("/areas/97", "/professional_roles", "/dictionaries"):
            pass  # open endpoints
        elif self.forbidden or auth != f"Bearer {self.accepted_token}":
            return httpx.Response(403, json={"errors": [{"type": "forbidden"}]})
        if request.url.path == "/areas/97":
            return httpx.Response(200, json=AREAS)
        if request.url.path == "/professional_roles":
            return httpx.Response(200, json=ROLES)
        if request.url.path == "/vacancies":
            self.searches.append(_multi(request))
            items = list(self.items.values())
            return httpx.Response(
                200, json={"items": items, "found": 137, "pages": 7, "page": 0, "per_page": 20}
            )
        if request.url.path.startswith("/employers/"):
            employer_id = request.url.path.rsplit("/", 1)[-1]
            if employer_id in self.employers:
                return httpx.Response(200, json=self.employers[employer_id])
            return httpx.Response(404, json={"errors": [{"type": "not_found"}]})
        vid = request.url.path.rsplit("/", 1)[-1]
        if vid in self.details:
            return httpx.Response(200, json=self.details[vid])
        return httpx.Response(404, json={"errors": [{"type": "not_found"}]})


def _multi(request: httpx.Request) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in request.url.params.multi_items():
        out.setdefault(key, []).append(value)
    return out


def _provider(board: Board, *, token: str | None = "app-token") -> HeadHunterProvider:
    http = httpx.AsyncClient(transport=httpx.MockTransport(board.handler))
    client = HhClient(
        http, base_url="https://api.hh.test", host="hh.uz", user_agent=UA, token=token
    )
    return HeadHunterProvider(client, JsonCache(None), area_id=97, search_ttl=60, detail_ttl=60)


@pytest.fixture
def configured(monkeypatch):
    """A token pasted in by the operator, so no exchange is needed."""
    monkeypatch.setattr(settings, "hh_access_token", "app-token")
    monkeypatch.setattr(settings, "hh_client_id", None)
    monkeypatch.setattr(settings, "hh_client_secret", None)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_a_listing_is_read_exactly_as_published():
    item = _item(
        "10",
        "Бухгалтер",
        salary={"from": 6000000, "to": 9000000, "currency": "UZS", "gross": False},
    )
    item["work_format"] = [{"id": "HYBRID", "name": "Гибрид"}]
    item["professional_roles"] = [{"id": "18", "name": "Бухгалтер"}]
    item["employer"]["logo_urls"] = {
        "90": "https://img.hh.ru/logo90.png",
        "240": "https://img.hh.ru/logo240.png",
    }
    item["employer"]["trusted"] = True
    summary = summary_from(item)
    assert summary.provider == "headhunter"
    assert summary.id == "10" and summary.title == "Бухгалтер" and summary.language == "ru"
    assert summary.salary is not None
    assert (summary.salary.amount_from, summary.salary.amount_to, summary.salary.currency) == (
        6000000,
        9000000,
        "UZS",
    )
    assert summary.salary.gross is False
    assert summary.experience == "between1And3"
    assert summary.employment == "full"
    assert summary.schedule == "fullDay"
    assert summary.work_formats == ("HYBRID",)
    assert summary.area == "Ташкент" and summary.region == "tashkent_city"
    assert summary.published_at == datetime(2026, 10, 30, 5, 0, tzinfo=UTC)
    assert summary.employer.name == "Alfa Savdo"
    assert summary.employer.logo_url == "https://img.hh.ru/logo240.png"
    assert summary.employer.trusted is True
    assert summary.url == "https://hh.uz/vacancy/10"
    assert summary.snippet == "Ведение учёта Знание Excel и 1С"
    assert summary.professional_roles == ("Бухгалтер",)
    assert summary.is_internship is False
    assert summary.is_active is True


def test_no_salary_stays_no_salary():
    assert salary_from(None) is None
    assert salary_from({"from": None, "to": None, "currency": "UZS"}) is None
    only_to = salary_from({"from": None, "to": 300, "currency": "RUR", "gross": True})
    assert only_to is not None and (only_to.amount_from, only_to.amount_to, only_to.currency) == (
        None,
        300,
        "RUB",
    )


def test_an_internship_is_flagged_from_any_of_its_signs():
    assert summary_from(_item("1", "Стажер в отдел продаж")).is_internship
    assert summary_from(_item("2", "Ассистент", internship=True)).is_internship
    probation = _item("3", "Ассистент")
    probation["employment"] = {"id": "probation", "name": "Стажировка"}
    assert summary_from(probation).is_internship


def test_an_uzbek_listing_reads_in_the_reformed_alphabet():
    item = _item("12", "Sotuvchi (doʻkon)")
    detail = detail_from(_detail(item, description="<p>Ish tajribasi shart emas</p>"))
    assert detail.language == "uz"
    assert detail.title == "Sotuvçi (dökon)"
    assert detail.description_html == "<p>Iş tajribasi şart emas</p>"


def test_the_full_record_carries_skills_languages_and_the_employer_page():
    item = _item("10", "Бухгалтер", archived=True)
    payload = _detail(item)
    payload["languages"] = [{"id": "rus", "name": "Русский", "level": {"id": "c1", "name": "C1"}}]
    payload["address"] = {"raw": "Ташкент, Мирабадский район"}
    payload["has_test"] = True
    employer = {
        "id": "1",
        "name": "Alfa Savdo",
        "description": '<p>Торговая компания.</p><script>alert(1)</script><img src="x">',
        "industries": [{"id": "41", "name": "Розничная торговля"}],
        "site_url": "https://alfa.uz",
    }
    detail = detail_from(payload, employer)
    assert detail.is_active is False
    assert detail.skills == ("Excel", "1С: Бухгалтерия")
    assert detail.languages == ("Русский — C1",)
    assert detail.address == "Ташкент, Мирабадский район"
    assert detail.has_test is True
    assert (
        detail.description_html
        == "<p>Обязанности:</p><ul><li>Учёт</li><li>Отчётность &amp; налоги</li></ul>"
    )
    assert detail.description_text.startswith("Обязанности:")
    assert detail.employer.description_html == "<p>Торговая компания.</p>"
    assert detail.employer.industries == ("Розничная торговля",)
    assert detail.employer.site_url == "https://alfa.uz"


def test_publication_times_in_the_boards_format_are_read():
    assert parse_when("2026-10-30T10:00:00+0500") == datetime(2026, 10, 30, 5, 0, tzinfo=UTC)
    assert parse_when("2026-10-30T10:00:00+05:00") == datetime(2026, 10, 30, 5, 0, tzinfo=UTC)
    assert parse_when("yesterday") is None
    assert parse_when(None) is None


def test_the_sanitiser_keeps_structure_and_drops_everything_else():
    markup = (
        '<h2 class="x">Обязанности</h2><p style="color:red">Учёт <b>и</b> <a href="http://x">отчётность</a></p>'
        "<script>alert(1)</script><ul><li>Один<li>Два</ul><img src=x><iframe src=y></iframe>"
    )
    assert sanitize_html(markup) == (
        "<h3>Обязанности</h3><p>Учёт <strong>и</strong> отчётность</p>"
        "<ul><li>Один</li><li>Два</li></ul>"
    )
    assert sanitize_html("Просто текст") == "<p>Просто текст</p>"
    assert sanitize_html("") == ""
    assert sanitize_html("<p>a &amp; b &lt; c</p>") == "<p>a &amp; b &lt; c</p>"


# ---------------------------------------------------------------------------
# Shortcuts
# ---------------------------------------------------------------------------


def test_picks_become_filters_without_overriding_her_own():
    query = VacancyQuery(
        text="бухгалтер", picks=["no_experience", "part_time", "junior"]
    ).resolve_picks()
    assert query.experience == "noExperience"
    assert query.employment == "part"
    assert query.text == "(бухгалтер) AND (junior OR младший OR помощник OR ассистент OR стажер)"

    kept = VacancyQuery(experience="moreThan6", picks=["no_experience", "remote"]).resolve_picks()
    assert kept.experience == "moreThan6"
    assert kept.schedule == "remote"
    assert VacancyQuery(picks=["internship"]).resolve_picks().employment == "probation"


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_her_filters_become_the_boards_parameters_and_nothing_else(configured):
    board = Board()
    board.add(_item("1", "Бухгалтер"))
    provider = _provider(board)

    page = await provider.search(
        VacancyQuery(
            text="бухгалтер",
            region="tashkent_city",
            salary_from=5000000,
            only_with_salary=True,
            experience="noExperience",
            employment="part",
            work_format="REMOTE",
            professional_roles=["18", "134"],
            sort="recent",
            page=2,
            per_page=10,
        )
    )
    params = board.searches[-1]
    assert params["text"] == ["бухгалтер"]
    # The region became the areas of the board's tree that belong to it —
    # Tashkent city is the city alone; Chirchiq belongs to the region.
    assert params["area"] == ["2759"]
    assert params["salary"] == ["5000000"] and params["only_with_salary"] == ["true"]
    assert params["experience"] == ["noExperience"] and params["employment"] == ["part"]
    assert params["work_format"] == ["REMOTE"]
    assert params["professional_role"] == ["18", "134"]
    assert params["order_by"] == ["publication_time"]
    assert params["page"] == ["1"] and params["per_page"] == ["10"]
    assert params["host"] == ["hh.uz"]
    assert "schedule" not in params
    assert page.total == 137 and page.pages == 7 and page.page == 2
    assert page.items[0].title == "Бухгалтер"


@pytest.mark.asyncio
async def test_without_a_query_the_newest_come_first_over_the_whole_country(configured):
    board = Board()
    provider = _provider(board)
    await provider.search(VacancyQuery())
    params = board.searches[-1]
    assert params["order_by"] == ["publication_time"]
    assert params["area"] == ["97"]
    assert "text" not in params


@pytest.mark.asyncio
async def test_an_identical_query_is_answered_from_cache(configured):
    board = Board()
    board.add(_item("1", "Бухгалтер"))
    provider = _provider(board)
    await provider.search(VacancyQuery(text="бухгалтер"))
    await provider.search(VacancyQuery(text="бухгалтер"))
    assert board.calls.count("/vacancies") == 1
    await provider.search(VacancyQuery(text="кассир"))
    assert board.calls.count("/vacancies") == 2


@pytest.mark.asyncio
async def test_a_refused_call_is_retried_once_with_a_fresh_token(monkeypatch):
    monkeypatch.setattr(settings, "hh_access_token", None)
    monkeypatch.setattr(settings, "hh_client_id", "id")
    monkeypatch.setattr(settings, "hh_client_secret", "secret")
    monkeypatch.setattr(hh_jobs, "_token", {"value": "stale-token", "expires": 10**12})
    board = Board()
    board.add(_item("1", "Бухгалтер"))
    provider = _provider(board, token=None)

    page = await provider.search(VacancyQuery(text="бухгалтер"))
    assert page.items[0].id == "1"
    assert board.calls.count("/token") == 1
    assert board.calls.count("/vacancies") == 2


@pytest.mark.asyncio
async def test_without_credentials_the_board_is_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "hh_access_token", None)
    monkeypatch.setattr(settings, "hh_client_id", None)
    monkeypatch.setattr(settings, "hh_client_secret", None)
    with pytest.raises(ProviderUnconfigured):
        await _provider(Board(), token=None).search(VacancyQuery())


@pytest.mark.asyncio
async def test_a_deleted_listing_is_none_and_an_archived_one_is_closed(configured):
    board = Board()
    gone = _item("2", "Кассир", archived=True)
    board.add(gone)
    board.employers["1"] = {"id": "1", "name": "Alfa Savdo", "industries": [{"name": "Торговля"}]}
    provider = _provider(board)

    assert await provider.get("404") is None
    detail = await provider.get("2")
    assert detail is not None and detail.is_active is False
    assert detail.employer.industries == ("Торговля",)
    # The listing and its employer are each read once.
    await provider.get("2")
    assert board.calls.count("/vacancies/2") == 1
    assert board.calls.count("/employers/1") == 1


@pytest.mark.asyncio
async def test_a_listing_stands_when_the_employer_page_cannot_be_read(configured):
    board = Board()
    board.add(_item("3", "Повар"))
    detail = await _provider(board).get("3")
    assert detail is not None and detail.employer.name == "Alfa Savdo"
    assert detail.employer.industries == ()


@pytest.mark.asyncio
async def test_regions_and_categories_come_from_the_boards_own_lists(configured):
    provider = _provider(Board())
    assert await provider.regions() == ("tashkent_city", "tashkent_region", "samarkand")
    groups = await provider.role_groups()
    assert [(g.id, g.name, g.role_ids) for g in groups] == [
        ("3", "Финансы, бухгалтерия", ("18", "134")),
        ("11", "Информационные технологии", ("96",)),
    ]
