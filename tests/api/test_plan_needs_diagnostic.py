"""No diagnostic, no plan.

A starter plan drafted before any assessment read as a verdict on someone the
platform knew nothing about; the roadmap is built from the diagnostic, so it
only exists after one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.config import settings
from app.models.assessment import Assessment
from app.models.plan import DevelopmentPlan
from app.models.user import User


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


async def test_without_a_diagnostic_no_plan_is_drafted(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()

    response = await client.post(
        "/api/v1/plans/generate", json={"horizon": "3m", "language": "uz"}, headers=auth_headers
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "diagnostic_required"
    count = await session.scalar(
        select(func.count()).select_from(DevelopmentPlan).where(DevelopmentPlan.user_id == user_id)
    )
    assert count == 0


async def test_the_refusal_does_not_spend_the_ai_quota(
    client, session, auth_headers, user_id, monkeypatch
):
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()
    monkeypatch.setattr(settings, "ai_user_hourly_limit", 1)

    for _ in range(3):
        refused = await client.post(
            "/api/v1/plans/generate", json={"horizon": "1m", "language": "uz"}, headers=auth_headers
        )
        assert refused.status_code == 409

    session.add(Assessment(user_id=user_id, completed_at=datetime.now(UTC)))
    await session.flush()
    allowed = await client.post(
        "/api/v1/plans/generate", json={"horizon": "1m", "language": "uz"}, headers=auth_headers
    )
    assert allowed.status_code == 201


async def test_an_unfinished_diagnostic_is_not_enough(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()
    session.add(Assessment(user_id=user_id, started_at=datetime.now(UTC)))
    await session.flush()

    response = await client.post(
        "/api/v1/plans/generate", json={"horizon": "3m", "language": "uz"}, headers=auth_headers
    )

    assert response.status_code == 409


async def test_after_the_diagnostic_a_plan_is_drafted(client, session, auth_headers, user_id):
    session.add(User(id=user_id, phone=_phone()))
    await session.flush()
    session.add(Assessment(user_id=user_id, completed_at=datetime.now(UTC)))
    await session.flush()

    response = await client.post(
        "/api/v1/plans/generate", json={"horizon": "3m", "language": "uz"}, headers=auth_headers
    )

    assert response.status_code == 201
    assert response.json()["is_active"] is False
