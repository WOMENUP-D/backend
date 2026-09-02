"""Structured JSON logging with PII scrubbing.

Every log line is machine-readable and carries the request id, so an audit
trail can be reconstructed from logs alone (section 12 of the specification).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id", default=None)

# Patterns scrubbed before anything reaches a log sink or an AI prompt.
_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\+998\d{9}"), "<phone>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
    (re.compile(r"\b\d{14}\b"), "<pinfl>"),  # personal identification number
    (re.compile(r"\b\d{16}\b"), "<card>"),
)


def scrub_pii(text: str) -> str:
    """Replace direct identifiers with type placeholders."""
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub_pii(record.getMessage()),
        }
        if rid := request_id_ctx.get():
            payload["request_id"] = rid
        if uid := user_id_ctx.get():
            payload["user_id"] = uid
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # uvicorn duplicates access logs in its own format; route them through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = [handler]
        logging.getLogger(name).propagate = False
