"""The configured vacancy provider.

`settings.vacancy_provider` names the board the marketplace reads from.
Today `headhunter` is the only one; a Tijorat Markazi provider registers its
name here when its labour-market API exists, and nothing above this module
changes.
"""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.services.hh_jobs import HhClient
from app.services.vacancies.base import (
    ProviderError,
    ProviderUnavailable,
    ProviderUnconfigured,
    VacancyProvider,
)
from app.services.vacancies.cache import JsonCache
from app.services.vacancies.headhunter import HeadHunterProvider

__all__ = [
    "ProviderError",
    "ProviderUnavailable",
    "ProviderUnconfigured",
    "VacancyProvider",
    "get_provider",
    "reset_provider",
]

_provider: VacancyProvider | None = None
_http: httpx.AsyncClient | None = None


def _build(name: str) -> VacancyProvider:
    global _http
    if name == "headhunter":
        if _http is None:
            _http = httpx.AsyncClient(timeout=settings.integration_timeout_seconds)
        client = HhClient(
            _http,
            base_url=settings.hh_api_base_url,
            host=settings.hh_host,
            user_agent=settings.hh_user_agent,
            token=settings.hh_access_token,
        )
        return HeadHunterProvider(
            client,
            JsonCache(str(settings.redis_url) if settings.redis_url else None),
            area_id=settings.hh_area_id,
            search_ttl=settings.vacancy_search_cache_seconds,
            detail_ttl=settings.vacancy_detail_cache_seconds,
        )
    raise ProviderUnconfigured(f"unknown vacancy provider: {name!r}")


def get_provider() -> VacancyProvider:
    """The board the marketplace reads from, built once per process."""
    global _provider
    if _provider is None:
        _provider = _build(settings.vacancy_provider)
    return _provider


def reset_provider() -> None:
    """Forget the built provider — for tests, and for a settings change."""
    global _provider
    _provider = None
