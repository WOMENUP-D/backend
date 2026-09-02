"""Consent is required before any data leaves the platform."""

import uuid

import pytest
import pytest_asyncio

from app.core.constants import ConsentScope, IntegrationSystem, Role
from app.models.user import User, UserRole
from app.services.audit_service import has_consent, record_consent
from app.services.integration_gateway import ConsentMissingError, queue_event


@pytest_asyncio.fixture
async def db_user(session) -> User:
    """A persisted user — consent rows carry a real foreign key."""
    user = User(phone="+998901112233")
    session.add(user)
    await session.flush()
    session.add(UserRole(user_id=user.id, role=Role.USER))
    await session.flush()
    return user


async def test_consent_defaults_to_absent(session):
    assert not await has_consent(session, uuid.uuid4(), ConsentScope.SHARE_EDU_JOB)


async def test_latest_record_wins_so_withdrawal_takes_effect(session, db_user):
    user_id = db_user.id
    await record_consent(
        session,
        user_id=user_id,
        scope=ConsentScope.SHARE_EDU_JOB,
        accepted=True,
        policy_version="1.0",
    )
    await session.flush()
    assert await has_consent(session, user_id, ConsentScope.SHARE_EDU_JOB)

    await record_consent(
        session,
        user_id=user_id,
        scope=ConsentScope.SHARE_EDU_JOB,
        accepted=False,
        policy_version="1.0",
    )
    await session.flush()
    assert not await has_consent(session, user_id, ConsentScope.SHARE_EDU_JOB)


async def test_queueing_without_consent_is_refused(session):
    with pytest.raises(ConsentMissingError):
        await queue_event(
            session,
            system=IntegrationSystem.EDU_JOB,
            event_type="user.sync",
            user_id=uuid.uuid4(),
            payload={"womanup_id": "x"},
        )


async def test_queueing_is_idempotent_for_the_same_reference(session, db_user):
    user_id = db_user.id
    await record_consent(
        session,
        user_id=user_id,
        scope=ConsentScope.SHARE_EDU_JOB,
        accepted=True,
        policy_version="1.0",
    )
    await session.flush()

    first = await queue_event(
        session,
        system=IntegrationSystem.EDU_JOB,
        event_type="user.sync",
        user_id=user_id,
        reference="ref-1",
        payload={},
    )
    await session.flush()
    second = await queue_event(
        session,
        system=IntegrationSystem.EDU_JOB,
        event_type="user.sync",
        user_id=user_id,
        reference="ref-1",
        payload={},
    )
    assert first.id == second.id
