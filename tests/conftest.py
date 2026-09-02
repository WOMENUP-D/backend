"""Test fixtures.

The models use PostgreSQL-specific types (JSONB, ARRAY, INET, pgvector), so
database-backed tests need a real PostgreSQL. Point TEST_DATABASE_URL at one;
when it is unreachable those tests skip and the pure-logic suite still runs.

The AI layer is switched off for the whole suite — see `disable_llm` below.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.constants import Role
from app.core.security import create_token
from app.db import get_session
from app.main import create_app
from app.models import Base
from app.services.llm_gateway import llm_gateway

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/womanup_test",
)


@pytest.fixture(autouse=True)
def disable_llm(monkeypatch):
    """No test may call the model provider.

    `Settings` reads the real `.env`, so on a developer machine with a key
    configured any endpoint that reaches the AI layer would quietly make a paid
    network request during `pytest` — and its assertions would then depend on
    what a model happened to say that afternoon. Blanking the key here routes
    every such endpoint down its documented fallback instead, which is the
    behaviour worth testing anyway.

    A test that wants the model enabled substitutes its own gateway with
    `monkeypatch.setattr`, as the news-editor tests do.
    """
    monkeypatch.setattr(llm_gateway, "_api_key", None, raising=False)


@pytest_asyncio.fixture
async def engine():
    """Schema created per test, dropped afterwards."""
    engine = create_async_engine(TEST_DB_URL, echo=False, poolclass=None)
    try:
        async with engine.begin() as connection:
            # pgvector must exist before the knowledge tables are created.
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.run_sync(Base.metadata.create_all)
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL unavailable at {TEST_DB_URL}: {exc}")

    yield engine

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def app_client() -> AsyncIterator[AsyncClient]:
    """Client for routes that never touch the database (health, schema, 401s)."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Database-backed client sharing the test's transaction."""
    app = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http

    app.dependency_overrides.clear()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_token(user_id, Role.USER, "access", {"roles": [Role.USER.value]})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers() -> dict[str, str]:
    token = create_token(uuid.uuid4(), Role.ADMIN, "access", {"roles": [Role.ADMIN.value]})
    return {"Authorization": f"Bearer {token}"}
