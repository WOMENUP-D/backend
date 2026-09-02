"""LLM gateway — the single outbound path to the model provider.

Everything AI-facing in the portal goes through here so that PII scrubbing,
prompt/model versioning and trace capture happen in exactly one place
(section 06: "Model versiyasi, prompt versiyasi va response trace saqlanishi").
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import anthropic

from app.core.config import settings
from app.core.logging import scrub_pii

logger = logging.getLogger(__name__)

# Bump when any system prompt below changes — persisted with every interaction.
PROMPT_VERSION = "2026.08.1"


class LlmUnavailableError(RuntimeError):
    """Raised when the model cannot be reached or is not configured."""


@dataclass(slots=True)
class LlmResponse:
    text: str
    trace_id: str
    model: str
    prompt_version: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str | None = None
    refused: bool = False
    parsed: dict[str, Any] | None = None
    raw_blocks: list[Any] = field(default_factory=list)


class LlmGateway:
    """Thin async wrapper over the Anthropic Messages API."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key or settings.anthropic_api_key
        self.model = model or settings.ai_model
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if not self.enabled:
            raise LlmUnavailableError(
                "ANTHROPIC_API_KEY is not configured; the AI layer is disabled"
            )
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        effort: str | None = None,
        json_schema: dict[str, Any] | None = None,
        cache_system: bool = True,
    ) -> LlmResponse:
        """One request/response round-trip.

        `json_schema` constrains the reply to that shape, which is how the
        roadmap and recommendation features get machine-readable output
        instead of prose they would have to parse heuristically.

        The system prompt is marked cacheable: it is long, identical across
        users, and sits before the volatile per-user content in the prefix.
        """
        trace_id = uuid.uuid4().hex
        started = time.perf_counter()

        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache_system:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or settings.ai_max_tokens,
            "system": system_blocks,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort or settings.ai_effort},
        }
        if json_schema is not None:
            request["output_config"]["format"] = {
                "type": "json_schema",
                "schema": json_schema,
            }

        try:
            response = await self.client.messages.create(**request)
        except anthropic.RateLimitError as exc:
            logger.warning("llm rate limited", extra={"extra_fields": {"trace_id": trace_id}})
            raise LlmUnavailableError("model rate limit reached") from exc
        except anthropic.APIStatusError as exc:
            logger.error(
                "llm api error",
                extra={"extra_fields": {"trace_id": trace_id, "status": exc.status_code}},
            )
            raise LlmUnavailableError(f"model returned {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LlmUnavailableError("could not reach the model provider") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        # A safety decline arrives as HTTP 200 with stop_reason "refusal" —
        # check it before reading content.
        refused = response.stop_reason == "refusal"
        text = "".join(block.text for block in response.content if block.type == "text")

        parsed: dict[str, Any] | None = None
        if json_schema is not None and text and not refused:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                logger.error(
                    "llm returned unparsable json",
                    extra={"extra_fields": {"trace_id": trace_id}},
                )

        return LlmResponse(
            text=text,
            trace_id=trace_id,
            model=response.model,
            prompt_version=PROMPT_VERSION,
            latency_ms=latency_ms,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
            refused=refused,
            parsed=parsed,
            raw_blocks=list(response.content),
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed text for the RAG index.

        Not implemented yet: the embedding provider is a separate procurement
        decision (data residency review, see the strategy document). Wire the
        chosen provider in here — `KnowledgeChunk.embedding` and
        `settings.embedding_dimensions` already expect vectors of a fixed size.
        """
        raise NotImplementedError(
            "Embedding provider is not configured. Implement LlmGateway.embed "
            "once the provider has passed the data-residency review."
        )

    @staticmethod
    def sanitise(text: str) -> str:
        """Strip direct identifiers before text reaches the model or a log."""
        return scrub_pii(text)


llm_gateway = LlmGateway()
