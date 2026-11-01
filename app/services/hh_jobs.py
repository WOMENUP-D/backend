"""Vacancies from hh.uz — read into the catalogue, never written back.

The opportunities table was empty on the live site: no partner feed delivers
listings yet. Until one does, the worker reads open vacancies for Uzbekistan
from the public hh API (api.hh.ru, which serves hh.uz too) into the same table
under the source `hh_uz`, so the jobs page, the skill matching and the course
suggestions have real listings to work on.

Three things this module is careful about:

* **Nothing leaves.** The feed is read-only. A woman applies on hh.uz, through
  the listing's own link, and WomanUP sends hh.uz nothing — not an
  application, not a profile, not a click. That is why `hh_uz` has no partner
  system and no consent scope, and why `services.opportunities.apply` refuses
  these listings outright instead of queueing anything.
* **Idempotent and honest.** `source + external_id` is the key: a vacancy the
  search returns again is refreshed, one it stops returning is taken down
  after `hh_sync_stale_days`, one the API marks archived is taken down at
  once. A run that could not search at all changes nothing — a failed run
  must never empty the catalogue.
* **Within the API's terms.** Every request carries the identifying agent the
  API asks for and the application token it now requires for vacancy search
  (registered at dev.hh.ru, exchanged with client credentials). Listings keep
  their link back to hh.uz and are shown with their source.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import OpportunitySource, OpportunityType, Region
from app.models.opportunity import Opportunity
from app.services.uzbek import looks_uzbek, to_new_alphabet

logger = logging.getLogger(__name__)

#: The audit action every run writes — also the schedule marker (`worker`).
AUDIT_ACTION = "opportunities.hh_sync"
PER_PAGE = 50
MAX_SKILLS = 20
MAX_DESCRIPTION = 6000
#: A pause between vacancy reads, so a run of three hundred is not a burst.
PAUSE_SECONDS = 0.2


class HhError(RuntimeError):
    """The API answered with an error."""

    def __init__(self, status: int, kind: str) -> None:
        super().__init__(f"hh api returned {status} ({kind})")
        self.status = status
        self.kind = kind


class HhForbidden(HhError):
    """Refused: no application token, or one that has expired. A
    configuration problem for the operator, not something to retry blindly."""


class HhNotFound(HhError):
    """The board no longer has it."""


@dataclass(slots=True)
class SyncResult:
    """What one run did — counts, plus why it did nothing when it did nothing."""

    seen: int = 0
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    failed: int = 0
    #: `unconfigured`, `forbidden`, `error`, `locked`, `disabled` — or None.
    reason: str | None = None


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


class HhClient:
    """The few calls this feed needs, over one `httpx.AsyncClient`."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        host: str,
        user_agent: str,
        token: str | None = None,
    ) -> None:
        self._http = http
        self.base_url = base_url.rstrip("/")
        self.host = host
        self.user_agent = user_agent
        self.token = token

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "HH-User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def authorise(self, client_id: str, client_secret: str) -> str:
        """Exchange the application's credentials for an application token.

        The token lives about two weeks and the API issues a new one at most
        every few minutes, so the caller keeps it (`app_token` below).
        """
        response = await self._http.post(
            f"{self.base_url}/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"User-Agent": self.user_agent, "HH-User-Agent": self.user_agent},
        )
        if response.status_code >= 400:
            raise HhError(response.status_code, "token")
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise HhError(response.status_code, "token")
        self.token = token
        return token

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One GET against the API, with its errors translated."""
        response = await self._http.get(
            f"{self.base_url}{path}", params=params or {}, headers=self.headers
        )
        if response.status_code == 403:
            raise HhForbidden(403, "forbidden")
        if response.status_code == 404:
            raise HhNotFound(404, "not_found")
        if response.status_code >= 400:
            kind = "error"
            try:
                errors = response.json().get("errors") or []
                if errors and isinstance(errors[0], dict):
                    kind = str(errors[0].get("type") or kind)
            except ValueError:
                pass
            raise HhError(response.status_code, kind)
        try:
            payload = response.json()
        except ValueError as exc:
            raise HhError(response.status_code, "bad_json") from exc
        if not isinstance(payload, dict):
            raise HhError(response.status_code, "bad_json")
        return payload

    async def search_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """One page of vacancies for an arbitrary set of search parameters.
        The site and the "listings only" switch are always added."""
        return await self.get(
            "/vacancies",
            # Listings only; hh's own advertising blocks are not vacancies.
            {**params, "host": self.host, "no_magic": "true"},
        )

    async def search(
        self, text: str, *, area: int, page: int = 0, per_page: int = PER_PAGE
    ) -> dict[str, Any]:
        """One page of open vacancies matching `text` in `area`."""
        return await self.search_params(
            {"text": text, "area": area, "page": page, "per_page": per_page}
        )

    async def vacancy(self, vacancy_id: str) -> dict[str, Any]:
        """One vacancy in full: the description and the key skills live here."""
        return await self.get(f"/vacancies/{vacancy_id}", {"host": self.host})

    async def employer(self, employer_id: str) -> dict[str, Any]:
        """An employer's public page: description, industries, site."""
        return await self.get(f"/employers/{employer_id}", {"host": self.host})

    async def areas(self, area_id: int) -> dict[str, Any]:
        """The area tree under one region or country."""
        return await self.get(f"/areas/{area_id}")

    async def professional_roles(self) -> dict[str, Any]:
        """The board's professional categories and the roles in them."""
        return await self.get("/professional_roles")


# The application token, kept for the life of the process. It is the same for
# every run, and the API would refuse a client that asked for a fresh one on
# each six-hourly tick.
_token: dict[str, Any] = {"value": None, "expires": 0.0}


async def app_token(client: HhClient, *, force: bool = False) -> str | None:
    """The token to send: the one configured by hand, else one exchanged for
    the application's credentials and cached until it expires."""
    if settings.hh_access_token:
        return settings.hh_access_token
    if not (settings.hh_client_id and settings.hh_client_secret):
        return None
    if not force and _token["value"] and _token["expires"] > time.monotonic():
        return _token["value"]
    token = await client.authorise(settings.hh_client_id, settings.hh_client_secret)
    # The API quotes about two weeks; renew a day early rather than a second late.
    _token["value"] = token
    _token["expires"] = time.monotonic() + 13 * 86400
    return token


def client_from_settings(http: httpx.AsyncClient) -> HhClient:
    return HhClient(
        http,
        base_url=settings.hh_api_base_url,
        host=settings.hh_host,
        user_agent=settings.hh_user_agent,
        token=settings.hh_access_token,
    )


# ---------------------------------------------------------------------------
# Reading one vacancy
# ---------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</(p|div|ul|ol|h[1-6]|tr|table|blockquote)>|<br\s*/?>", re.I)
_LIST_ITEM = re.compile(r"<li[^>]*>", re.I)
_BLANK_LINES = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t ]+")
_INTERNSHIP = re.compile(r"стаж[её]р|стажир|intern|stajy?or|amaliyot", re.I)

#: hh's Uzbekistan area tree names its cities and regions in Russian. Mapped
#: to the fourteen regions the platform knows; a town it has not seen stays
#: unmapped and the listing reads "nationwide" rather than a wrong region.
_REGION_BY_AREA: dict[str, Region] = {
    "ташкент": Region.TASHKENT_CITY,
    "ташкентская область": Region.TASHKENT_REGION,
    "чирчик": Region.TASHKENT_REGION,
    "ангрен": Region.TASHKENT_REGION,
    "алмалык": Region.TASHKENT_REGION,
    "бекабад": Region.TASHKENT_REGION,
    "янгиюль": Region.TASHKENT_REGION,
    "нурафшон": Region.TASHKENT_REGION,
    "газалкент": Region.TASHKENT_REGION,
    "ахангаран": Region.TASHKENT_REGION,
    "андижан": Region.ANDIJAN,
    "андижанская область": Region.ANDIJAN,
    "асака": Region.ANDIJAN,
    "бухара": Region.BUKHARA,
    "бухарская область": Region.BUKHARA,
    "каган": Region.BUKHARA,
    "фергана": Region.FERGANA,
    "ферганская область": Region.FERGANA,
    "коканд": Region.FERGANA,
    "маргилан": Region.FERGANA,
    "кувасай": Region.FERGANA,
    "джизак": Region.JIZZAKH,
    "джизакская область": Region.JIZZAKH,
    "нукус": Region.KARAKALPAKSTAN,
    "каракалпакстан": Region.KARAKALPAKSTAN,
    "республика каракалпакстан": Region.KARAKALPAKSTAN,
    "карши": Region.KASHKADARYA,
    "кашкадарьинская область": Region.KASHKADARYA,
    "шахрисабз": Region.KASHKADARYA,
    "ургенч": Region.KHOREZM,
    "хорезмская область": Region.KHOREZM,
    "хива": Region.KHOREZM,
    "наманган": Region.NAMANGAN,
    "наманганская область": Region.NAMANGAN,
    "чуст": Region.NAMANGAN,
    "навои": Region.NAVOI,
    "навоийская область": Region.NAVOI,
    "зарафшан": Region.NAVOI,
    "самарканд": Region.SAMARKAND,
    "самаркандская область": Region.SAMARKAND,
    "каттакурган": Region.SAMARKAND,
    "гулистан": Region.SIRDARYA,
    "сырдарьинская область": Region.SIRDARYA,
    "янгиер": Region.SIRDARYA,
    "термез": Region.SURKHANDARYA,
    "сурхандарьинская область": Region.SURKHANDARYA,
    "денау": Region.SURKHANDARYA,
}


@dataclass(slots=True)
class Listing:
    """A vacancy as the catalogue stores it."""

    external_id: str
    title: str
    language: str
    description: str
    organisation: str | None
    region: str | None
    skills: list[str]
    reward: dict[str, Any]
    type: OpportunityType
    external_url: str | None
    archived: bool


def strip_html(text: str | None) -> str:
    """The text of an hh description: paragraphs kept, bullets kept, tags gone."""
    if not text:
        return ""
    cleaned = _LIST_ITEM.sub("\n• ", text)
    cleaned = _BLOCK_END.sub("\n", cleaned)
    cleaned = _TAG.sub("", cleaned)
    cleaned = html.unescape(cleaned)
    lines = [_SPACES.sub(" ", line).strip() for line in cleaned.split("\n")]
    joined = "\n".join(lines)
    return _BLANK_LINES.sub("\n\n", joined).strip()


def regions_by_area() -> dict[str, str]:
    """hh area name (folded) -> platform region key, for the marketplace's
    location filter to turn a region into the board's area ids."""
    return {name: region.value for name, region in _REGION_BY_AREA.items()}


def region_for(area_name: str | None) -> str | None:
    if not area_name:
        return None
    key = area_name.strip().casefold()
    # "Акташ (Узбекистан)" and the like.
    key = re.sub(r"\s*\(.*\)$", "", key)
    region = _REGION_BY_AREA.get(key)
    return region.value if region else None


def reward_for(salary: dict[str, Any] | None) -> dict[str, Any]:
    """A salary as the listing page reads it: `salary_from`, `salary_to`,
    `currency`. Empty when the vacancy states none."""
    if not isinstance(salary, dict):
        return {}
    reward: dict[str, Any] = {}
    for key, target in (("from", "salary_from"), ("to", "salary_to")):
        value = salary.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            reward[target] = int(value)
    if reward:
        currency = str(salary.get("currency") or "UZS").upper()
        # hh writes roubles as RUR; the page knows the ISO code.
        reward["currency"] = "RUB" if currency == "RUR" else currency
    return reward


def _name(value: Any) -> str:
    return str(value.get("name") or "").strip() if isinstance(value, dict) else ""


def facts_line(item: dict[str, Any]) -> str:
    """Experience, employment, schedule and format, in the API's own words."""
    parts = [_name(item.get("experience")), _name(item.get("employment"))]
    schedule = _name(item.get("schedule"))
    formats = [_name(entry) for entry in item.get("work_format") or [] if _name(entry)]
    parts.extend(formats or [schedule])
    return " · ".join(part for part in parts if part)


def snippet_text(item: dict[str, Any]) -> str:
    snippet = item.get("snippet") or {}
    pieces = []
    for key in ("responsibility", "requirement"):
        value = snippet.get(key) if isinstance(snippet, dict) else None
        if value:
            pieces.append(strip_html(str(value)))
    return "\n\n".join(piece for piece in pieces if piece)


def looks_like_internship(title: str | None) -> bool:
    """Whether a title says "intern" in any of the three languages."""
    return bool(title) and bool(_INTERNSHIP.search(title))


def kind_for(item: dict[str, Any], detail: dict[str, Any] | None) -> OpportunityType:
    if (detail or {}).get("internship") is True or item.get("internship") is True:
        return OpportunityType.INTERNSHIP
    if looks_like_internship(str(item.get("name") or "")):
        return OpportunityType.INTERNSHIP
    return OpportunityType.VACANCY


def skills_for(detail: dict[str, Any] | None) -> list[str]:
    seen: set[str] = set()
    skills: list[str] = []
    for entry in (detail or {}).get("key_skills") or []:
        name = _name(entry)
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            skills.append(name[:100])
        if len(skills) >= MAX_SKILLS:
            break
    return skills


def parse_listing(item: dict[str, Any], detail: dict[str, Any] | None = None) -> Listing:
    """A search result — and, when read in full, the vacancy — as a listing.

    Uzbek titles and descriptions are written in the reformed alphabet, as
    everything on the platform is; Russian ones are kept as written.
    """
    title = str(item.get("name") or "").strip()[:300]
    body = strip_html((detail or {}).get("description")) or snippet_text(item)
    facts = facts_line(detail or item)
    description = "\n\n".join(part for part in (facts, body) if part)[:MAX_DESCRIPTION]

    language = "uz" if looks_uzbek(title) else "ru"
    if language == "uz":
        title = to_new_alphabet(title)
        description = to_new_alphabet(description)

    employer = item.get("employer") or {}
    organisation = _name(employer)[:200] or None
    return Listing(
        external_id=str(item.get("id") or ""),
        title=title,
        language=language,
        description=description,
        organisation=organisation,
        region=region_for(_name(item.get("area"))),
        skills=skills_for(detail),
        reward=reward_for(item.get("salary")),
        type=kind_for(item, detail),
        external_url=str(item.get("alternate_url") or "")[:500] or None,
        archived=bool(item.get("archived")),
    )


# ---------------------------------------------------------------------------
# The sync
# ---------------------------------------------------------------------------


async def sync_vacancies(
    session: AsyncSession,
    client: HhClient,
    *,
    queries: list[str],
    area: int,
    pages: int = 1,
    max_new: int = 300,
    stale_days: int = 10,
    pause: float = PAUSE_SECONDS,
    now: datetime | None = None,
) -> SyncResult:
    """One run: search, read what is new, refresh what is known, retire what
    is gone. Flushes; the caller commits.

    A run in which no search succeeded returns `reason="error"` and touches
    nothing — in particular it retires nothing, because "the API was down" and
    "every vacancy closed" must not look the same to the catalogue.
    """
    now = now or datetime.now(UTC)
    result = SyncResult()

    seen: dict[str, dict[str, Any]] = {}
    searched = 0
    for query in queries:
        for page in range(max(1, pages)):
            try:
                payload = await client.search(query, area=area, page=page)
            except HhForbidden:
                logger.error(
                    "hh.uz refused the search: an application token is needed "
                    "(HH_CLIENT_ID/HH_CLIENT_SECRET or HH_ACCESS_TOKEN)"
                )
                result.reason = "forbidden"
                return result
            except (HhError, httpx.HTTPError) as exc:
                logger.warning("hh.uz search failed for %r: %s", query, exc)
                result.failed += 1
                break
            searched += 1
            for item in payload.get("items") or []:
                vacancy_id = str(item.get("id") or "")
                if vacancy_id and vacancy_id not in seen:
                    seen[vacancy_id] = item
            if page + 1 >= int(payload.get("pages") or 0):
                break

    result.seen = len(seen)
    if not searched:
        result.reason = "error"
        return result

    existing: dict[str, Opportunity] = {}
    if seen:
        rows = await session.execute(
            select(Opportunity).where(
                Opportunity.source == OpportunitySource.HH_UZ,
                Opportunity.external_id.in_(list(seen)),
            )
        )
        existing = {row.external_id: row for row in rows.scalars() if row.external_id}

    read = 0
    for vacancy_id, item in seen.items():
        row = existing.get(vacancy_id)
        if item.get("archived"):
            if row is not None and row.is_active:
                row.is_active = False
                result.deactivated += 1
            continue

        if row is None:
            if read >= max_new:
                continue
            try:
                detail = await client.vacancy(vacancy_id)
            except HhForbidden:
                result.reason = "forbidden"
                break
            except (HhError, httpx.HTTPError) as exc:
                # Created next time, whole; a listing without its skills and
                # description would be a poorer record than none.
                logger.warning("hh.uz vacancy %s could not be read: %s", vacancy_id, exc)
                result.failed += 1
                continue
            read += 1
            if pause:
                await asyncio.sleep(pause)
            listing = parse_listing(item, detail)
            if not listing.title or not listing.external_url:
                continue
            session.add(
                Opportunity(
                    source=OpportunitySource.HH_UZ,
                    external_id=listing.external_id,
                    type=listing.type,
                    title_i18n={listing.language: listing.title},
                    description_i18n={listing.language: listing.description},
                    organisation=listing.organisation,
                    region=listing.region,
                    required_skills=listing.skills,
                    eligibility={},
                    reward=listing.reward,
                    external_url=listing.external_url,
                    is_active=True,
                    synced_at=now,
                )
            )
            result.created += 1
        else:
            # A light refresh from the search result: what can change on a
            # live vacancy. The skills and the description were read once and
            # stay; so do the course suggestions built on them.
            listing = parse_listing(item, None)
            if listing.title:
                row.title_i18n = {listing.language: listing.title}
            row.organisation = listing.organisation
            row.region = listing.region
            row.reward = listing.reward
            row.external_url = listing.external_url or row.external_url
            row.type = listing.type
            row.is_active = True
            row.synced_at = now
            result.updated += 1

    await session.flush()

    cutoff = now - timedelta(days=max(1, stale_days))
    retired = await session.execute(
        update(Opportunity)
        .where(
            Opportunity.source == OpportunitySource.HH_UZ,
            Opportunity.is_active.is_(True),
            Opportunity.synced_at.is_not(None),
            Opportunity.synced_at < cutoff,
        )
        .values(is_active=False)
    )
    result.deactivated += retired.rowcount or 0
    return result


async def sync_from_settings(session: AsyncSession, *, pause: float = PAUSE_SECONDS) -> SyncResult:
    """A run with the operator's configuration, including the token exchange.

    Returns `reason="unconfigured"` when neither a token nor credentials are
    set — a worker without them has nothing to do and says so once per run.
    """
    async with httpx.AsyncClient(timeout=settings.integration_timeout_seconds) as http:
        client = client_from_settings(http)
        try:
            token = await app_token(client)
        except (HhError, httpx.HTTPError) as exc:
            logger.error("hh.uz token exchange failed: %s", exc)
            return SyncResult(reason="forbidden")
        if token is None:
            logger.warning("hh.uz sync is on but no token or credentials are configured")
            return SyncResult(reason="unconfigured")
        client.token = token

        result = await sync_vacancies(
            session,
            client,
            queries=list(settings.hh_search_queries),
            area=settings.hh_area_id,
            pages=settings.hh_sync_pages_per_query,
            max_new=settings.hh_sync_max_new,
            stale_days=settings.hh_sync_stale_days,
            pause=pause,
        )
        if result.reason == "forbidden" and not settings.hh_access_token:
            # The cached token may simply have expired: exchange once more
            # and try again, this run.
            try:
                client.token = await app_token(client, force=True)
            except (HhError, httpx.HTTPError) as exc:
                logger.error("hh.uz token exchange failed: %s", exc)
                return result
            result = await sync_vacancies(
                session,
                client,
                queries=list(settings.hh_search_queries),
                area=settings.hh_area_id,
                pages=settings.hh_sync_pages_per_query,
                max_new=settings.hh_sync_max_new,
                stale_days=settings.hh_sync_stale_days,
                pause=pause,
            )
        return result
