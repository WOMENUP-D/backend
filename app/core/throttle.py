"""A per-address ceiling on how fast the API may be called at all.

The first line against a runaway client or a small flood, in front of the
narrower limits each feature keeps (the database-backed counters on sign-in
and the assistant, the per-visitor spacing on analytics). In-process and
per worker, like the sign-in throttles: it protects one API process and
resets when it restarts, which is the right scale for a single-host
deployment behind a CDN — the CDN's own rate limiting and WAF stand in front
of it for anything larger (see DEPLOYMENT.md).

Generous on purpose. A whole office behind one address must never be refused
for using the portal; what is refused is a script making hundreds of calls a
minute from one place.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

WINDOW_SECONDS = 60.0
#: Keys tracked at most, so a flood of addresses cannot grow the map without
#: bound; the oldest are dropped first.
MAX_TRACKED = 50_000


@dataclass(slots=True)
class SlidingWindow:
    """How many events a key caused in the last minute."""

    limit: int
    window: float = WINDOW_SECONDS
    _hits: dict[str, deque[float]] = field(default_factory=dict)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        queue = self._hits.get(key)
        if queue is None:
            if len(self._hits) >= MAX_TRACKED:
                self._hits.pop(next(iter(self._hits)))
            queue = deque()
            self._hits[key] = queue
        cutoff = now - self.window
        while queue and queue[0] <= cutoff:
            queue.popleft()
        if len(queue) >= self.limit:
            return False
        queue.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


#: Tighter ceilings for the paths worth more to an attacker: the live job
#: board proxy (each distinct query is a call against the board's quota) and
#: the model-backed endpoints (each call costs money). Matched by prefix
#: under the API prefix; the hourly database counters still apply on top.
EXPENSIVE_PREFIXES = ("/vacancies", "/assistant", "/ai/")
