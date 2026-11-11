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
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.deps import client_ip
from app.api.router import api_router
from app.core import security_log
from app.core.config import settings
from app.core.logging import configure_logging, request_id_ctx, user_id_ctx
from app.core.throttle import EXPENSIVE_PREFIXES, SlidingWindow
from app.db import SessionLocal, engine
from app.schemas.common import ErrorResponse
from app.services import telemetry

logger = logging.getLogger(__name__)

#: Set on every response. The API serves JSON to a browser application on
#: another origin, so it is never framed, never sniffed into another type and
#: never tells a third party where the request came from.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}

#: Validation failures a client maps to its own message. When one of these is
#: among a 422's errors it becomes the response's `detail`.
STABLE_VALIDATION_CODES = frozenset({"weak_password", "email_domain_unsupported"})

#: PostgreSQL's SQLSTATE for a character text columns cannot hold (NUL).
CHARACTER_NOT_IN_REPERTOIRE = "22021"


def _carries_nul(request: Request) -> bool:
    """A NUL byte in the path or the query string, raw or percent-encoded.
    PostgreSQL text cannot hold one, so it can only ever end in an error."""
    raw = (request.scope.get("raw_path") or b"") + b"?" + (request.scope.get("query_string") or b"")
    return b"\x00" in raw or b"%00" in raw.lower()


def _request_id(request: Request) -> str | None:
    """The request's id, also after the middleware has left its context."""
    return getattr(request.state, "request_id", None) or request_id_ctx.get()


class BodyLimitMiddleware:
    """Counts the bytes a request body actually streams, not only what its
    `Content-Length` declares — a chunked upload declares nothing.

    Past `MAX_REQUEST_BODY_BYTES` the app is told the client went away (so
    nothing more is read or parsed) and whatever it answers is replaced by a
    413. Nothing is raised through the stack: an exception thrown from
    `receive` reaches the app wrapped in other errors and comes out as a 400.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        received = 0
        exceeded = False
        replied = False

        async def counting_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > settings.max_request_body_bytes:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal replied
            if not exceeded:
                await send(message)
                return
            if replied:
                return
            replied = True
            response = JSONResponse(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content=ErrorResponse(
                    detail="Request body too large", code="body_too_large"
                ).model_dump(),
            )
            await response(scope, receive, send)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not replied:
            await guarded_send({"type": "http.response.start"})


#: Per-address ceilings, rebuilt by `create_app` so tests get fresh ones.
general_throttle = SlidingWindow(settings.rate_limit_per_minute)
expensive_throttle = SlidingWindow(settings.expensive_rate_limit_per_minute)


def _throttled(request: Request) -> bool:
    """Whether this request exceeds its address's allowance for the minute."""
    path = request.url.path
    if not path.startswith(settings.api_v1_prefix):
        return False
    key = client_ip(request) or "unknown"
    general_throttle.limit = settings.rate_limit_per_minute
    expensive_throttle.limit = settings.expensive_rate_limit_per_minute
    if not general_throttle.allow(key):
        return True
    relative = path[len(settings.api_v1_prefix) :]
    return any(relative.startswith(prefix) for prefix in EXPENSIVE_PREFIXES) and (
        not expensive_throttle.allow(key)
    )


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
        # The schema maps the whole attack surface; like the docs it is a
        # development aid, not a public page.
        openapi_url=None if settings.is_production else f"{settings.api_v1_prefix}/openapi.json",
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        lifespan=lifespan,
    )

    if settings.allowed_hosts and settings.allowed_hosts != ["*"]:
        # Refused before routing: a request for a host the API does not serve
        # is not one of ours, whatever the proxy passed through.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Attach a request id to logs and to the response, refuse oversized
        bodies, and set the headers every API answer carries."""
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        # Kept on the request too: the context is reset when this middleware
        # returns, which is before an exception handler writes its answer.
        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)
        user_token = user_id_ctx.set(None)
        try:
            if _throttled(request):
                security_log.event("api.rate_limited", path=request.url.path, method=request.method)
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    headers={"Retry-After": "60", "X-Request-ID": request_id},
                    content=ErrorResponse(
                        detail="Too many requests", code="rate_limited", request_id=request_id
                    ).model_dump(),
                )
            if _carries_nul(request):
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    headers={"X-Request-ID": request_id},
                    content=ErrorResponse(
                        detail="Validation failed", code="validation_error", request_id=request_id
                    ).model_dump(),
                )
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > settings.max_request_body_bytes:
                return JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content=ErrorResponse(
                        detail="Request body too large",
                        code="body_too_large",
                        request_id=request_id,
                    ).model_dump(),
                )
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            for name, value in SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
            if settings.is_production:
                response.headers.setdefault(
                    "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                )
            # API answers are personal and never cacheable by a shared proxy
            # or by the browser's back button.
            if request.url.path.startswith(settings.api_v1_prefix):
                response.headers.setdefault("Cache-Control", "no-store")
            if response.status_code >= 500:
                _spawn(record_api_error(request, response.status_code))
            return response
        finally:
            request_id_ctx.reset(token)
            user_id_ctx.reset(user_token)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Where and why, never what was sent: Pydantic's `input` echoes the
        # submitted value — a password that failed a length rule included —
        # and `ctx` can carry internal objects.
        errors = [
            {key: value for key, value in error.items() if key in ("loc", "msg", "type")}
            for error in exc.errors()
        ]
        # A rule the person can act on names itself in `detail`, so a client
        # can show "choose a harder password" instead of "something failed".
        detail = next(
            (error["type"] for error in errors if error.get("type") in STABLE_VALIDATION_CODES),
            "Validation failed",
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=ErrorResponse(
                detail=detail,
                code="validation_error",
                request_id=_request_id(request),
            ).model_dump()
            # jsonable_encoder is required: raw errors embed exception objects.
            | {"errors": jsonable_encoder(errors)},
        )

    @app.exception_handler(DBAPIError)
    async def database_error_handler(request: Request, exc: DBAPIError) -> JSONResponse:
        """A NUL byte in a JSON string reaches the database and is refused
        there; that is the client's input, a 422 — not a 500 that fills the
        error dashboard. Any other database error is a 500 as before."""
        orig = exc.orig
        code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
        cause = getattr(orig, "__cause__", None)
        code = code or getattr(cause, "sqlstate", None)
        if code == CHARACTER_NOT_IN_REPERTOIRE:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content=ErrorResponse(
                    detail="Validation failed",
                    code="validation_error",
                    request_id=_request_id(request),
                ).model_dump(),
            )
        return await unhandled_handler(request, exc)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        """Never leak an internal error message to the client."""
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=ErrorResponse(
                detail="Internal server error",
                code="internal_error",
                request_id=_request_id(request),
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
    # Outermost, so the count covers every byte before anything reads it.
    app.add_middleware(BodyLimitMiddleware)
    return app


app = create_app()
