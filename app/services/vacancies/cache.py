"""A small JSON cache for provider answers.

A job board's search is the same for everyone who types the same thing, and
its dictionaries change once a year, so answers are kept for a while: a page
of results for minutes, one listing for a quarter of an hour, the area tree
and the role list for a day. That keeps the portal well inside the board's
request quota and makes the second visitor's page instant.

Redis when it is configured and reachable, process memory otherwise — the
portal must not need Redis to show a vacancy. Nothing personal is ever
cached here: the keys are made of the query, never of who asked.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

MEMORY_LIMIT = 500
RETRY_REDIS_AFTER = 60.0


class JsonCache:
    def __init__(self, redis_url: str | None, *, prefix: str = "vacancies:") -> None:
        self._url = redis_url
        self._prefix = prefix
        self._redis: Any = None
        self._redis_failed_at = 0.0
        self._memory: dict[str, tuple[float, Any]] = {}

    async def _client(self) -> Any:
        if not self._url:
            return None
        if self._redis is not None:
            return self._redis
        if time.monotonic() - self._redis_failed_at < RETRY_REDIS_AFTER:
            return None
        try:
            from redis import asyncio as aioredis

            client = aioredis.from_url(self._url, decode_responses=True)
            await client.ping()
        except Exception as exc:  # noqa: BLE001 - any failure means "no Redis for now"
            self._redis_failed_at = time.monotonic()
            logger.info("vacancy cache running in memory: %s", exc)
            return None
        self._redis = client
        return client

    async def get(self, key: str) -> Any | None:
        client = await self._client()
        if client is not None:
            try:
                raw = await client.get(self._prefix + key)
                if raw is not None:
                    return json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                logger.info("vacancy cache read failed, using memory: %s", exc)
                self._redis = None
                self._redis_failed_at = time.monotonic()
        entry = self._memory.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires < time.monotonic():
            self._memory.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if len(self._memory) >= MEMORY_LIMIT:
            now = time.monotonic()
            for stale in [k for k, (expires, _) in self._memory.items() if expires < now]:
                self._memory.pop(stale, None)
            if len(self._memory) >= MEMORY_LIMIT:
                self._memory.pop(next(iter(self._memory)), None)
        self._memory[key] = (time.monotonic() + ttl, value)
        client = await self._client()
        if client is not None:
            try:
                await client.set(self._prefix + key, json.dumps(value, ensure_ascii=False), ex=ttl)
            except Exception as exc:  # noqa: BLE001
                logger.info("vacancy cache write failed, kept in memory: %s", exc)
                self._redis = None
                self._redis_failed_at = time.monotonic()

    def clear(self) -> None:
        self._memory.clear()
