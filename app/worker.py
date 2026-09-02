"""Background worker: notification delivery, outbox drain, risk flags.

Run alongside the API (`python -m app.worker`). Kept as a simple polling loop
rather than a broker-backed queue — at pilot volume this is enough, and it has
no infrastructure the pilot does not already need.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.constants import RiskFlagType
from app.core.logging import configure_logging
from app.db import SessionLocal
from app.models.audit import RiskFlag
from app.models.user import User
from app.services.integration_gateway import drain_outbox
from app.services.notification_service import dispatch_pending

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
RISK_SCAN_SECONDS = 3600
INACTIVITY_DAYS = 21


async def scan_inactivity() -> int:
    """Raise a risk flag for users inactive beyond the threshold.

    The flag notifies a coordinator. It never restricts the user's access —
    section 06 is explicit that there is no automatic sanction.
    """
    cutoff = datetime.now(UTC) - timedelta(days=INACTIVITY_DAYS)

    async with SessionLocal() as session:
        candidates = list(
            (
                await session.execute(
                    select(User).where(
                        User.last_active_at.is_not(None),
                        User.last_active_at < cutoff,
                        User.onboarding_completed_at.is_not(None),
                    )
                )
            ).scalars()
        )

        raised = 0
        for user in candidates:
            already = await session.scalar(
                select(RiskFlag.id).where(
                    RiskFlag.user_id == user.id,
                    RiskFlag.flag_type == RiskFlagType.INACTIVITY,
                    RiskFlag.resolved_at.is_(None),
                )
            )
            if already:
                continue
            session.add(
                RiskFlag(
                    user_id=user.id,
                    flag_type=RiskFlagType.INACTIVITY,
                    severity="medium",
                    reason=f"No activity for more than {INACTIVITY_DAYS} days",
                    evidence={"last_active_at": user.last_active_at.isoformat()},
                )
            )
            raised += 1

        await session.commit()
        return raised


async def run() -> None:
    configure_logging()
    logger.info("worker started (poll=%ss)", POLL_SECONDS)
    last_risk_scan = 0.0

    while True:
        try:
            async with SessionLocal() as session:
                delivered = await dispatch_pending(session)
                sent, failed = await drain_outbox(session)
                await session.commit()

            if delivered or sent or failed:
                logger.info(
                    "worker tick",
                    extra={
                        "extra_fields": {
                            "notifications": delivered,
                            "integration_sent": sent,
                            "integration_failed": failed,
                        }
                    },
                )

            now = asyncio.get_running_loop().time()
            if now - last_risk_scan >= RISK_SCAN_SECONDS:
                raised = await scan_inactivity()
                last_risk_scan = now
                if raised:
                    logger.info("raised %s inactivity flags", raised)

        except Exception:
            logger.exception("worker tick failed; continuing")

        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run())
