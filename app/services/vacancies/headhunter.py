"""HeadHunter (hh.uz) as a `VacancyProvider`.

Reads the board through its public API (api.hh.ru serves hh.uz with
`host=hh.uz`) and answers in the portal's own vacancy types. Nothing is
written to the board and nothing about the reader is sent to it: the search
parameters are the filters she chose, and the one personal act — applying —
happens on hh.uz, through the listing's own link.

What the API requires, and how it is handled here:

* An identifying agent on every request, and an application token for
  vacancy search and employer pages (anonymous search is refused with 403).
  The token is exchanged from the application's credentials registered at
  dev.hh.ru and kept for the process; a refused call is retried once with a
  fresh token before it is reported as unavailable.
* Results are the board's own: the salary as the employer wrote it (with its
  currency and gross flag), the publication time, the structured experience,
  employment, schedule and work-format values, the key skills. A field the
  board leaves empty stays empty.
* A listing the board has archived keeps `is_active=False`; one it has
  deleted is `None`.

Every answer is cached (`cache.JsonCache`) for a short while, so the board
is asked once per distinct query, not once per visitor.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import settings
from app.services import hh_jobs
from app.services.hh_jobs import HhClient, HhError, HhForbidden, HhNotFound
from app.services.uzbek import looks_uzbek, to_new_alphabet
from app.services.vacancies.base import (
    Employer,
    ProviderUnavailable,
    ProviderUnconfigured,
    RoleGroup,
    Salary,
    VacancyDetail,
    VacancyPage,
    VacancyProvider,
    VacancyQuery,
    VacancySummary,
)
from app.services.vacancies.cache import JsonCache
from app.services.vacancies.text import sanitize_html

logger = logging.getLogger(__name__)

NAME = "headhunter"
LABEL = "HeadHunter"
MAX_PER_PAGE = 50
#: hh pages are zero-based and the API serves at most 2000 results per query.
MAX_PAGES = 40
DAY = 86400

_ORDER = {"relevance": "relevance", "recent": "publication_time", "salary": "salary_desc"}


def _name(value: Any) -> str:
    return str(value.get("name") or "").strip() if isinstance(value, dict) else ""


def safe_url(value: Any) -> str | None:
    """A web address or nothing. The board's data is untrusted input: only
    `http(s)` with a host survives, so no `javascript:` or `data:` value ever
    reaches an `href` or an `<img src>`."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return text[:500]


def _id(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("id") is not None:
        return str(value["id"])
    return None


def parse_when(value: Any) -> datetime | None:
    """hh writes `2026-10-30T10:00:00+0500`; the offset has no colon."""
    if not isinstance(value, str) or not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(value, fmt).astimezone(UTC)
        except ValueError:
            continue
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(UTC)


def salary_from(value: Any) -> Salary | None:
    """The employer's figures, exactly. None when the listing states none."""
    if not isinstance(value, dict):
        return None
    amounts: dict[str, int | None] = {}
    for key in ("from", "to"):
        raw = value.get(key)
        amounts[key] = (
            int(raw)
            if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0
            else None
        )
    if amounts["from"] is None and amounts["to"] is None:
        return None
    currency = str(value.get("currency") or "").upper() or None
    if currency == "RUR":
        currency = "RUB"
    gross = value.get("gross")
    return Salary(
        amount_from=amounts["from"],
        amount_to=amounts["to"],
        currency=currency,
        gross=gross if isinstance(gross, bool) else None,
    )


def employer_from(value: Any, full: dict[str, Any] | None = None) -> Employer:
    value = value if isinstance(value, dict) else {}
    logos = value.get("logo_urls") if isinstance(value.get("logo_urls"), dict) else {}
    logo = logos.get("240") or logos.get("90") or logos.get("original")
    full = full or {}
    industries = tuple(_name(entry) for entry in (full.get("industries") or []) if _name(entry))
    trusted = value.get("trusted")
    return Employer(
        id=_id(value),
        name=_name(value) or "—",
        url=safe_url(value.get("alternate_url")),
        logo_url=safe_url(logo),
        description_html=sanitize_html(full.get("description")) or None,
        industries=industries,
        site_url=safe_url(full.get("site_url")),
        trusted=trusted if isinstance(trusted, bool) else None,
    )


def _text_language(*texts: str) -> str:
    return "uz" if any(looks_uzbek(text) for text in texts if text) else "ru"


def _localise(text: str, language: str) -> str:
    return to_new_alphabet(text) if language == "uz" else text


def summary_from(item: dict[str, Any]) -> VacancySummary:
    title = str(item.get("name") or "").strip()
    language = _text_language(title)
    snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
    pieces = [
        hh_jobs.strip_html(str(snippet.get(key) or "")) for key in ("responsibility", "requirement")
    ]
    employment = item.get("employment")
    return VacancySummary(
        provider=NAME,
        id=str(item.get("id") or ""),
        title=_localise(title, language),
        employer=employer_from(item.get("employer")),
        salary=salary_from(item.get("salary")),
        area=_name(item.get("area")) or None,
        region=hh_jobs.region_for(_name(item.get("area"))),
        work_formats=tuple(
            str(entry.get("id")) for entry in (item.get("work_format") or []) if _id(entry)
        ),
        schedule=_id(item.get("schedule")),
        employment=_id(employment),
        experience=_id(item.get("experience")),
        published_at=parse_when(item.get("published_at")),
        snippet=_localise(" ".join(piece for piece in pieces if piece)[:400], language),
        url=safe_url(item.get("alternate_url"))
        or f"https://{settings.hh_host}/vacancy/{item.get('id')}",
        language=language,
        is_internship=bool(item.get("internship"))
        or _id(employment) == "probation"
        or hh_jobs.looks_like_internship(title),
        professional_roles=tuple(
            _name(entry) for entry in (item.get("professional_roles") or []) if _name(entry)
        ),
        is_active=not bool(item.get("archived")),
    )


def detail_from(
    payload: dict[str, Any], employer_full: dict[str, Any] | None = None
) -> VacancyDetail:
    base = summary_from(payload)
    address = payload.get("address") if isinstance(payload.get("address"), dict) else {}
    fields = {name: getattr(base, name) for name in VacancySummary.__slots__}
    fields["employer"] = employer_from(payload.get("employer"), employer_full)
    return VacancyDetail(
        **fields,
        description_html=_localise(sanitize_html(payload.get("description")), base.language),
        description_text=_localise(hh_jobs.strip_html(payload.get("description")), base.language),
        skills=tuple(_name(entry) for entry in (payload.get("key_skills") or []) if _name(entry)),
        languages=tuple(
            " — ".join(part for part in (_name(entry), _name(entry.get("level"))) if part)
            for entry in (payload.get("languages") or [])
            if isinstance(entry, dict) and _name(entry)
        ),
        address=str(address.get("raw") or "") or None,
        has_test=bool(payload.get("has_test")),
    )


def _key(kind: str, payload: Any) -> str:
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(), usedforsecurity=False
    ).hexdigest()
    return f"hh:{kind}:{digest}"


class HeadHunterProvider(VacancyProvider):
    name = NAME
    label = LABEL

    def __init__(
        self,
        client: HhClient,
        cache: JsonCache,
        *,
        area_id: int,
        search_ttl: int = 300,
        detail_ttl: int = 900,
    ) -> None:
        self.client = client
        self.cache = cache
        self.area_id = area_id
        self.search_ttl = search_ttl
        self.detail_ttl = detail_ttl
        self.site_url = f"https://{client.host}"

    # -- the API, with the token and the error translation -----------------

    async def _ensure_token(self, *, force: bool = False) -> None:
        try:
            token = await hh_jobs.app_token(self.client, force=force)
        except (HhError, httpx.HTTPError) as exc:
            raise ProviderUnavailable(f"token exchange failed: {exc}") from exc
        if token is None:
            raise ProviderUnconfigured(
                "HeadHunter needs HH_CLIENT_ID/HH_CLIENT_SECRET or HH_ACCESS_TOKEN"
            )
        self.client.token = token

    async def _call(self, method: str, *args: Any) -> dict[str, Any]:
        """One API call: the token first, one retry with a fresh token on a
        refusal, and the board's failures as provider errors."""
        await self._ensure_token()
        try:
            return await getattr(self.client, method)(*args)
        except HhForbidden:
            if settings.hh_access_token:
                raise ProviderUnconfigured("HeadHunter refused the configured token") from None
            await self._ensure_token(force=True)
            try:
                return await getattr(self.client, method)(*args)
            except HhForbidden as exc:
                raise ProviderUnconfigured("HeadHunter refused the application token") from exc
            except HhNotFound:
                raise
            except (HhError, httpx.HTTPError) as exc:
                raise ProviderUnavailable(str(exc)) from exc
        except HhNotFound:
            raise
        except (HhError, httpx.HTTPError) as exc:
            raise ProviderUnavailable(str(exc)) from exc

    async def _cached(self, key: str, ttl: int, method: str, *args: Any) -> dict[str, Any]:
        hit = await self.cache.get(key)
        if isinstance(hit, dict):
            return hit
        payload = await self._call(method, *args)
        await self.cache.set(key, payload, ttl)
        return payload

    # -- the area tree, for the location filter ---------------------------

    async def _area_ids_by_region(self) -> dict[str, list[str]]:
        tree = await self._cached(f"hh:areas:{self.area_id}", DAY, "areas", self.area_id)
        by_area = hh_jobs.regions_by_area()
        out: dict[str, list[str]] = {}

        def walk(node: dict[str, Any]) -> None:
            name = _name(node).casefold()
            name = name.split(" (")[0]
            region = by_area.get(name)
            if region and node.get("id") is not None:
                out.setdefault(region, []).append(str(node["id"]))
            for child in node.get("areas") or []:
                walk(child)

        walk(tree)
        return out

    async def regions(self) -> tuple[str, ...]:
        known = await self._area_ids_by_region()
        return tuple(region.value for region in hh_jobs.Region if region.value in known)

    # -- the provider --------------------------------------------------------

    async def search(self, query: VacancyQuery) -> VacancyPage:
        query = query.resolve_picks()
        per_page = max(1, min(query.per_page, MAX_PER_PAGE))
        page = max(1, min(query.page, MAX_PAGES))
        params: dict[str, Any] = {"page": page - 1, "per_page": per_page}
        params["order_by"] = (
            _ORDER[query.sort] if query.text or query.sort != "relevance" else ("publication_time")
        )
        if query.text:
            params["text"] = query.text
        area_ids: list[str] = []
        if query.region:
            area_ids = (await self._area_ids_by_region()).get(query.region, [])
        params["area"] = area_ids or self.area_id
        if query.salary_from:
            params["salary"] = int(query.salary_from)
        if query.only_with_salary:
            params["only_with_salary"] = "true"
        for field in ("experience", "employment", "schedule", "work_format"):
            value = getattr(query, field)
            if value:
                params[field] = value
        if query.professional_roles:
            params["professional_role"] = list(query.professional_roles)

        payload = await self._cached(
            _key("search", params), self.search_ttl, "search_params", params
        )
        items = tuple(
            summary_from(item)
            for item in payload.get("items") or []
            if isinstance(item, dict) and item.get("id")
        )
        return VacancyPage(
            items=items,
            total=int(payload.get("found") or 0),
            page=page,
            pages=int(payload.get("pages") or 0),
            per_page=per_page,
        )

    async def get(self, vacancy_id: str) -> VacancyDetail | None:
        try:
            payload = await self._cached(
                f"hh:vacancy:{vacancy_id}", self.detail_ttl, "vacancy", vacancy_id
            )
        except HhNotFound:
            return None
        employer_full: dict[str, Any] | None = None
        employer_id = _id(payload.get("employer"))
        if employer_id:
            try:
                employer_full = await self._cached(
                    f"hh:employer:{employer_id}", DAY, "employer", employer_id
                )
            except (HhNotFound, ProviderUnavailable, ProviderUnconfigured) as exc:
                # The listing stands without the employer's page.
                logger.info("employer %s not read: %s", employer_id, exc)
        return detail_from(payload, employer_full)

    async def role_groups(self) -> tuple[RoleGroup, ...]:
        payload = await self._cached("hh:roles", DAY, "professional_roles")
        groups: list[RoleGroup] = []
        for category in payload.get("categories") or []:
            if not isinstance(category, dict) or category.get("id") is None:
                continue
            roles = tuple(
                str(role["id"])
                for role in (category.get("roles") or [])
                if isinstance(role, dict) and role.get("id") is not None
            )
            groups.append(RoleGroup(id=str(category["id"]), name=_name(category), role_ids=roles))
        return tuple(groups)
