"""The hh.uz feed: read faithfully, written idempotently, never sent anything.

Pinned here: how a vacancy is read (salary, region, kind, skills, text, the
alphabet of an Uzbek title); that a run creates, refreshes and retires the
right rows and nothing else; and that a run which could not search changes
nothing — "the API refused us" and "every vacancy closed" must never look the
same to the catalogue.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core.constants import OpportunitySource, OpportunityType
from app.models.opportunity import Opportunity
from app.services import hh_jobs
from app.services.hh_jobs import (
    HhClient,
    kind_for,
    parse_listing,
    region_for,
    reward_for,
    strip_html,
    sync_vacancies,
)

UA = "WomanUP/1.0 (test@womanup.uz)"


def _item(
    vid: str,
    name: str,
    *,
    area: str = "Ташкент",
    salary: dict | None = None,
    archived: bool = False,
    employer: str = "Alfa Savdo",
    internship: bool = False,
) -> dict:
    return {
        "id": vid,
        "name": name,
        "area": {"id": "2759", "name": area},
        "salary": salary,
        "archived": archived,
        "internship": internship,
        "employer": {"id": "1", "name": employer, "alternate_url": "https://hh.uz/employer/1"},
        "alternate_url": f"https://hh.uz/vacancy/{vid}",
        "published_at": "2026-10-30T10:00:00+0500",
        "snippet": {
            "requirement": "Знание <highlighttext>Excel</highlighttext> и 1С",
            "responsibility": "Ведение учёта",
        },
        "schedule": {"id": "fullDay", "name": "Полный день"},
        "experience": {"id": "between1And3", "name": "От 1 года до 3 лет"},
        "employment": {"id": "full", "name": "Полная занятость"},
    }


def _detail(item: dict, *, skills=("Excel", "1С: Бухгалтерия"), description: str | None = None):
    return {
        **item,
        "key_skills": [{"name": skill} for skill in skills],
        "description": description
        or "<p>Обязанности:</p><ul><li>Учёт</li><li>Отчётность &amp; налоги</li></ul>",
    }


class FakeHh:
    """An hh API in a box: a search index, details by id, and a switch that
    makes it refuse everything the way the real one does without a token."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.details: dict[str, dict] = {}
        self.forbidden = False
        self.broken = False
        self.calls: list[str] = []

    def add(self, item: dict, detail: dict | None = None) -> None:
        self.items[item["id"]] = item
        self.details[item["id"]] = detail or _detail(item)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        assert request.headers["HH-User-Agent"] == UA
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "app-token", "expires_in": 1209599})
        if self.forbidden or "Authorization" not in request.headers:
            return httpx.Response(403, json={"errors": [{"type": "forbidden"}]})
        if self.broken:
            return httpx.Response(502, text="bad gateway")
        if request.url.path == "/vacancies":
            items = list(self.items.values())
            return httpx.Response(
                200, json={"items": items, "found": len(items), "pages": 1, "page": 0}
            )
        vid = request.url.path.rsplit("/", 1)[-1]
        if vid in self.details:
            return httpx.Response(200, json=self.details[vid])
        return httpx.Response(404, json={"errors": [{"type": "not_found"}]})


def _client(fake: FakeHh, *, token: str | None = "app-token") -> HhClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return HhClient(http, base_url="https://api.hh.test", host="hh.uz", user_agent=UA, token=token)


# ---------------------------------------------------------------------------
# Reading one vacancy
# ---------------------------------------------------------------------------


def test_html_becomes_paragraphs_and_bullets():
    text = strip_html("<p>Обязанности:</p><ul><li>Учёт</li><li>Отчётность &amp; налоги</li></ul>")
    assert text == "Обязанности:\n\n• Учёт\n• Отчётность & налоги"
    assert strip_html(None) == ""


def test_salary_reads_as_the_listing_page_expects():
    assert reward_for({"from": 5000000, "to": 7000000, "currency": "UZS", "gross": False}) == {
        "salary_from": 5000000,
        "salary_to": 7000000,
        "currency": "UZS",
    }
    assert reward_for({"from": None, "to": 300, "currency": "RUR"}) == {
        "salary_to": 300,
        "currency": "RUB",
    }
    assert reward_for(None) == {}
    assert reward_for({"from": 0, "to": None, "currency": "UZS"}) == {}


def test_hh_areas_map_to_the_platform_regions_or_to_nowhere():
    assert region_for("Ташкент") == "tashkent_city"
    assert region_for("Самарканд") == "samarkand"
    assert region_for("Ферганская область") == "fergana"
    assert region_for("Нукус") == "karakalpakstan"
    # A town the platform does not know reads as nationwide, not as a guess.
    assert region_for("Акташ (Узбекистан)") is None
    assert region_for(None) is None


def test_internships_are_told_apart_from_vacancies():
    assert kind_for(_item("1", "Стажер-бухгалтер"), None) == OpportunityType.INTERNSHIP
    assert kind_for(_item("2", "Бухгалтер"), {"internship": True}) == OpportunityType.INTERNSHIP
    assert kind_for(_item("3", "Бухгалтер"), {"internship": False}) == OpportunityType.VACANCY


def test_a_russian_vacancy_is_read_whole():
    item = _item("10", "Бухгалтер", salary={"from": 6000000, "to": None, "currency": "UZS"})
    listing = parse_listing(item, _detail(item))
    assert listing.external_id == "10"
    assert listing.language == "ru"
    assert listing.title == "Бухгалтер"
    assert listing.skills == ["Excel", "1С: Бухгалтерия"]
    assert listing.organisation == "Alfa Savdo"
    assert listing.region == "tashkent_city"
    assert listing.reward == {"salary_from": 6000000, "currency": "UZS"}
    assert listing.external_url == "https://hh.uz/vacancy/10"
    assert listing.type == OpportunityType.VACANCY
    # The facts the API states, then the description — paragraphs kept.
    assert listing.description.startswith("От 1 года до 3 лет · Полная занятость · Полный день")
    assert "• Отчётность & налоги" in listing.description


def test_without_the_full_record_the_snippet_stands_in():
    listing = parse_listing(_item("11", "Кассир"), None)
    assert listing.skills == []
    assert "Ведение учёта" in listing.description
    assert "Знание Excel и 1С" in listing.description
    assert "<highlighttext>" not in listing.description


def test_an_uzbek_title_is_written_in_the_reformed_alphabet():
    item = _item("12", "Sotuvchi (doʻkon, Chilonzor)")
    listing = parse_listing(item, _detail(item, description="<p>Ish tajribasi shart emas</p>"))
    assert listing.language == "uz"
    assert listing.title == "Sotuvçi (dökon, Çilonzor)"
    assert "Iş tajribasi şart emas" in listing.description


# ---------------------------------------------------------------------------
# The sync
# ---------------------------------------------------------------------------


async def _hh_rows(session) -> dict[str, Opportunity]:
    rows = await session.execute(
        select(Opportunity).where(Opportunity.source == OpportunitySource.HH_UZ)
    )
    return {row.external_id: row for row in rows.scalars()}


@pytest.mark.asyncio
async def test_a_run_creates_refreshes_and_retires(session):
    fake = FakeHh()
    fake.add(_item("1", "Бухгалтер", salary={"from": 5000000, "to": None, "currency": "UZS"}))
    fake.add(_item("2", "Стажер в отдел продаж", area="Самарканд"))
    client = _client(fake)

    first = await sync_vacancies(session, client, queries=["бухгалтер"], area=97, pause=0)
    assert (first.seen, first.created, first.updated, first.deactivated) == (2, 2, 0, 0)
    assert first.reason is None

    rows = await _hh_rows(session)
    assert rows["1"].type == OpportunityType.VACANCY
    assert rows["1"].required_skills == ["Excel", "1С: Бухгалтерия"]
    assert rows["1"].external_url == "https://hh.uz/vacancy/1"
    assert rows["1"].reward["salary_from"] == 5000000
    assert rows["2"].type == OpportunityType.INTERNSHIP
    assert rows["2"].region == "samarkand"
    # Nothing is read twice: one search, one detail per new vacancy.
    assert fake.calls.count("/vacancies") == 1
    assert fake.calls.count("/vacancies/1") == 1

    # The second run: 1 has a new salary and is still open, 2 is archived,
    # 3 is new. The known vacancy is refreshed from the search result alone.
    fake.items["1"]["salary"] = {"from": 6000000, "to": 8000000, "currency": "UZS"}
    fake.items["2"]["archived"] = True
    fake.add(_item("3", "Оператор call-центра"))
    second = await sync_vacancies(session, client, queries=["бухгалтер"], area=97, pause=0)
    assert (second.created, second.updated, second.deactivated) == (1, 1, 1)

    rows = await _hh_rows(session)
    assert rows["1"].is_active and rows["1"].reward["salary_to"] == 8000000
    assert rows["1"].required_skills == ["Excel", "1С: Бухгалтерия"]
    assert not rows["2"].is_active
    assert rows["3"].is_active
    assert fake.calls.count("/vacancies/1") == 1
    assert fake.calls.count("/vacancies/3") == 1


@pytest.mark.asyncio
async def test_a_vacancy_the_searches_stopped_returning_is_retired_after_the_grace(session):
    fake = FakeHh()
    fake.add(_item("1", "Бухгалтер"))
    client = _client(fake)
    long_ago = datetime.now(UTC) - timedelta(days=30)
    session.add(
        Opportunity(
            source=OpportunitySource.HH_UZ,
            external_id="old",
            type=OpportunityType.VACANCY,
            title_i18n={"ru": "Давно закрытая"},
            required_skills=[],
            eligibility={},
            reward={},
            external_url="https://hh.uz/vacancy/old",
            is_active=True,
            synced_at=long_ago,
        )
    )
    session.add(
        Opportunity(
            source=OpportunitySource.HH_UZ,
            external_id="recent",
            type=OpportunityType.VACANCY,
            title_i18n={"ru": "Недавняя"},
            required_skills=[],
            eligibility={},
            reward={},
            external_url="https://hh.uz/vacancy/recent",
            is_active=True,
            synced_at=datetime.now(UTC) - timedelta(days=2),
        )
    )
    await session.flush()

    result = await sync_vacancies(
        session, client, queries=["бухгалтер"], area=97, stale_days=10, pause=0
    )
    assert result.deactivated == 1
    rows = await _hh_rows(session)
    assert not rows["old"].is_active
    assert rows["recent"].is_active
    assert rows["1"].is_active


@pytest.mark.asyncio
async def test_a_refused_run_changes_nothing(session):
    """Without a token the API answers 403. That is the operator's problem to
    fix, and the catalogue must look exactly as it did."""
    fake = FakeHh()
    fake.add(_item("1", "Бухгалтер"))
    await sync_vacancies(session, _client(fake), queries=["бухгалтер"], area=97, pause=0)

    fake.forbidden = True
    stale = datetime.now(UTC) - timedelta(days=40)
    (await _hh_rows(session))["1"].synced_at = stale
    await session.flush()

    result = await sync_vacancies(session, _client(fake), queries=["бухгалтер"], area=97, pause=0)
    assert result.reason == "forbidden"
    assert (result.created, result.updated, result.deactivated) == (0, 0, 0)
    assert (await _hh_rows(session))["1"].is_active


@pytest.mark.asyncio
async def test_a_run_in_which_every_search_failed_retires_nothing(session):
    fake = FakeHh()
    fake.add(_item("1", "Бухгалтер"))
    await sync_vacancies(session, _client(fake), queries=["бухгалтер"], area=97, pause=0)
    (await _hh_rows(session))["1"].synced_at = datetime.now(UTC) - timedelta(days=40)
    await session.flush()

    fake.broken = True
    result = await sync_vacancies(
        session, _client(fake), queries=["бухгалтер", "кассир"], area=97, pause=0
    )
    assert result.reason == "error"
    assert result.failed == 2
    assert (await _hh_rows(session))["1"].is_active


@pytest.mark.asyncio
async def test_a_vacancy_that_could_not_be_read_in_full_waits_for_the_next_run(session):
    fake = FakeHh()
    fake.add(_item("1", "Бухгалтер"))
    fake.items["2"] = _item("2", "Кассир")  # listed, but its record 404s
    result = await sync_vacancies(session, _client(fake), queries=["x"], area=97, pause=0)
    assert result.created == 1
    assert result.failed == 1
    assert set(await _hh_rows(session)) == {"1"}


@pytest.mark.asyncio
async def test_the_token_exchange_sends_client_credentials():
    fake = FakeHh()
    client = _client(fake, token=None)
    token = await client.authorise("id", "secret")
    assert token == "app-token"
    assert client.headers["Authorization"] == "Bearer app-token"
    assert fake.calls == ["/token"]


@pytest.mark.asyncio
async def test_the_application_token_is_kept_for_the_process(monkeypatch):
    from app.core.config import settings

    fake = FakeHh()
    client = _client(fake, token=None)
    monkeypatch.setattr(settings, "hh_access_token", None)
    monkeypatch.setattr(settings, "hh_client_id", "id")
    monkeypatch.setattr(settings, "hh_client_secret", "secret")
    monkeypatch.setattr(hh_jobs, "_token", {"value": None, "expires": 0.0})

    assert await hh_jobs.app_token(client) == "app-token"
    assert await hh_jobs.app_token(client) == "app-token"
    assert fake.calls.count("/token") == 1
    assert await hh_jobs.app_token(client, force=True) == "app-token"
    assert fake.calls.count("/token") == 2


@pytest.mark.asyncio
async def test_without_credentials_there_is_no_token(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "hh_access_token", None)
    monkeypatch.setattr(settings, "hh_client_id", None)
    monkeypatch.setattr(settings, "hh_client_secret", None)
    assert await hh_jobs.app_token(_client(FakeHh(), token=None)) is None


def test_the_search_request_names_the_site_and_the_area():
    fake = FakeHh()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return fake.handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = HhClient(http, base_url="https://api.hh.test", host="hh.uz", user_agent=UA, token="t")

    import asyncio

    asyncio.run(client.search("бухгалтер", area=97))
    params = dict(captured[-1].url.params)
    assert params["host"] == "hh.uz"
    assert params["area"] == "97"
    assert params["text"] == "бухгалтер"
    assert json.loads(fake.handler(captured[-1]).text)["pages"] == 1
