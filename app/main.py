"""Application factory and entrypoint."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging, request_id_ctx, user_id_ctx
from app.db import SessionLocal, engine
from app.schemas.common import ErrorResponse
from app.services import telemetry

logger = logging.getLogger(__name__)

# Fire-and-forget recorders for failed requests. Held here so the event loop
# does not collect them before they finish; removed as they complete.
_background: set[asyncio.Task] = set()


async def record_api_error(request: Request, status_code: int) -> None:
    """One `api_error` row for a request the API could not serve.

    The route template, never the path with ids in it; the status and the
    method; the visitor cookie so affected people can be counted. Its own
    session, because the request's is gone, and every failure swallowed: the
    error is already on its way to the client and the log.
    """
    route = request.scope.get("route")
    template = getattr(route, "path", None) or "unmatched"
    try:
        async with SessionLocal() as session:
            await telemetry.record_event(
                session,
                name="api_error",
                actor=telemetry.actor_of(request, None),
                properties={
                    "route": template[:120],
                    "status": status_code,
                    "method": request.method,
                },
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — never a second failure on top of the first
        logger.debug("could not record api_error for %s", template, exc_info=True)


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging("DEBUG" if settings.debug else "INFO")
    logger.info("starting %s in %s", settings.app_name, settings.environment)
    yield
    await engine.dispose()
    logger.info("shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "WomanUP — national digital ecosystem for the development of women "
            "and girls. Portal API (phase 1)."
        ),
        openapi_url=f"{settings.api_v1_prefix}/openapi.json",
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Attach a request id to logs and to the response."""
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        user_token = user_id_ctx.set(None)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            if response.status_code >= 500:
                _spawn(record_api_error(request, response.status_code))
            return response
        finally:
            request_id_ctx.reset(token)
            user_id_ctx.reset(user_token)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=ErrorResponse(
                detail="Validation failed",
                code="validation_error",
                request_id=request_id_ctx.get(),
            ).model_dump()
            # jsonable_encoder is required: raw errors embed exception objects.
            | {"errors": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        """Never leak an internal error message to the client."""
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=ErrorResponse(
                detail="Internal server error",
                code="internal_error",
                request_id=request_id_ctx.get(),
            ).model_dump(),
        )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "environment": settings.environment}

    @app.get("/health/ready", tags=["system"])
    async def readiness() -> JSONResponse:
        """Readiness probe — verifies the database is actually reachable."""
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as exc:
            logger.error("readiness check failed: %s", exc)
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unavailable", "database": "unreachable"},
            )
        return JSONResponse(content={"status": "ready", "database": "ok"})

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
