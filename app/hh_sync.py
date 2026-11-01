"""Read the hh.uz vacancy feed once, now: `python -m app.hh_sync`.

The worker does this on its own schedule when `HH_SYNC_ENABLED` is on. This
entry point is for the operator who has just configured the credentials and
wants the catalogue filled before the next tick — and for seeing, in the
printed result, whether the API accepted the token.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from app.core.logging import configure_logging
from app.worker import run_hh_sync


async def main() -> int:
    configure_logging()
    result = await run_hh_sync(force=True)
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0 if result.reason in (None, "locked") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
