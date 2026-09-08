"""LLM gateway — the single outbound path to the model provider.

Everything AI-facing in the portal goes through here so that PII scrubbing,
prompt/model versioning and trace capture happen in exactly one place
(section 06: "Model versiyasi, prompt versiyasi va response trace saqlanishi").

Server-side tools live here too. Web search and web fetch run on the
provider's infrastructure, so declaring them costs the portal no second
egress and no crawler of its own — which is what keeps the "one outbound
path" rule true while the news ingest still reads real pages. The block
shapes those tools return are provider knowledge, so the two readers below
(`search_hits`, `fetched_pages`) are the only place that touches them.
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
PROMPT_VERSION = "2026.09.1"

# Pinned tool versions. Both are the _20260209 generation, which carries its
# own dynamic filtering: `code_execution` must NOT be declared beside them, or
# the model is handed a second execution environment it does not need.
WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
WEB_FETCH_TOOL_TYPE = "web_fetch_20260209"


class LlmUnavailableError(RuntimeError):
    """Raised when the model cannot be reached or is not configured."""


@dataclass(frozen=True, slots=True)
class WebSearchHit:
    """One result the provider's search tool actually returned.

    Kept as a value object rather than a raw block so that callers never index
    provider-shaped content: the URL a caller is allowed to attribute a post to
    is exactly a URL that appears here.
    """

    title: str
    url: str
    page_age: str | None = None


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """The text of a page the fetch tool actually retrieved.

    This is what makes a grounded draft possible. A title and a URL are enough
    to invent an article from; only the page's own words are enough to
    summarise one, and this carries them.
    """

    url: str
    title: str
    text: str


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
    # How many times a `pause_turn` was resumed inside this one logical turn.
    # Part of the trace rather than a debugging counter: it is the difference
    # between "the model answered" and "the model answered on the fourth leg".
    resumes: int = 0


def _tools_emit_citations(tools: list[dict[str, Any]] | None) -> bool:
    """Whether any declared tool was asked to attach citations to its output.

    Narrow on purpose. The documented incompatibility with
    `output_config.format` is about *document citations* — a `document` block
    with `citations: {"enabled": true}`, which is also what `web_fetch` emits
    when citations are switched on. Server tools in general are not
    incompatible with structured output, so this must not grow into a blanket
    "tools and json_schema cannot ride together" rule: that would later reject
    a perfectly legal custom-tool + structured-output call.
    """
    for tool in tools or []:
        citations = tool.get("citations")
        if isinstance(citations, dict) and citations.get("enabled"):
            return True
        if citations is True:
            return True
    return False


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

    async def _send(self, request: dict[str, Any], trace_id: str) -> Any:
        """One HTTP round trip, with the provider's failures translated.

        Factored out of `complete` so the resume loop can make several of them
        under a single trace id without duplicating the error mapping.
        """
        try:
            return await self.client.messages.create(**request)
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

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        effort: str | None = None,
        json_schema: dict[str, Any] | None = None,
        cache_system: bool = True,
        tools: list[dict[str, Any]] | None = None,
        max_resumes: int = 0,
    ) -> LlmResponse:
        """One request/response round-trip.

        `json_schema` constrains the reply to that shape, which is how the
        roadmap and recommendation features get machine-readable output
        instead of prose they would have to parse heuristically.

        `tools` declares server-side tools (search, fetch). They run on the
        provider's side, so nothing is executed here — the results simply
        arrive as extra blocks in `raw_blocks`. A long tool turn stops with
        `stop_reason == "pause_turn"` at HTTP 200, which without `max_resumes`
        would be returned as a silently truncated answer; set it to the number
        of legs the caller is willing to pay for.

        The system prompt is marked cacheable: it is long, identical across
        users, and sits before the volatile per-user content in the prefix.
        """
        if json_schema is not None and _tools_emit_citations(tools):
            raise ValueError(
                "citations and json_schema cannot be combined: a cited reply "
                "carries citation metadata, which the API rejects alongside "
                "output_config.format. Gather cited material in one call and "
                "structure it in a second."
            )

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
        if tools:
            request["tools"] = tools

        turns: list[dict[str, Any]] = list(messages)
        blocks: list[Any] = []
        input_tokens = 0
        output_tokens = 0
        resumes = 0

        while True:
            request["messages"] = turns
            response = await self._send(request, trace_id)
            blocks.extend(response.content)
            input_tokens += response.usage.input_tokens or 0
            output_tokens += response.usage.output_tokens or 0
            if response.stop_reason != "pause_turn" or resumes >= max_resumes:
                break
            # Resume by re-sending the conversation plus this assistant turn
            # unchanged. No "Continue." message: the API resumes off the
            # trailing server_tool_use block and an extra turn confuses it.
            turns = [*turns, {"role": "assistant", "content": response.content}]
            resumes += 1

        # Measured across the whole resumed turn, and tokens summed over it, so
        # a paused turn is not reported as cheaper than it was.
        latency_ms = int((time.perf_counter() - started) * 1000)

        # A safety decline arrives as HTTP 200 with stop_reason "refusal" —
        # check it before reading content.
        refused = response.stop_reason == "refusal"
        text = "".join(block.text for block in blocks if block.type == "text")

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
            input_tokens=input_tokens or None,
            output_tokens=output_tokens or None,
            stop_reason=response.stop_reason,
            refused=refused,
            parsed=parsed,
            raw_blocks=blocks,
            resumes=resumes,
        )

    @staticmethod
    def web_search_tool(
        *, max_uses: int = 5, allowed_domains: list[str] | None = None
    ) -> dict[str, Any]:
        """The server-side search tool, pinned to a version.

        `allowed_domains` and `blocked_domains` are mutually exclusive, so only
        the allow form is offered here — a portal that names the sources it
        trusts is making a smaller promise than one that lists what it
        distrusts.
        """
        tool: dict[str, Any] = {
            "type": WEB_SEARCH_TOOL_TYPE,
            "name": "web_search",
            "max_uses": max_uses,
        }
        if allowed_domains:
            tool["allowed_domains"] = list(allowed_domains)
        return tool

    @staticmethod
    def web_fetch_tool(
        *,
        max_uses: int = 5,
        allowed_domains: list[str] | None = None,
        max_content_tokens: int | None = None,
    ) -> dict[str, Any]:
        """The server-side fetch tool, for URLs already in the conversation.

        Declared beside search rather than instead of it: search returns a
        title and a link, and a title and a link are enough to invent an
        article from. Citations are deliberately left off — they are what the
        API rejects alongside a structured-output format, and the caller
        structures the result in a second call.
        """
        tool: dict[str, Any] = {
            "type": WEB_FETCH_TOOL_TYPE,
            "name": "web_fetch",
            "max_uses": max_uses,
        }
        if allowed_domains:
            tool["allowed_domains"] = list(allowed_domains)
        if max_content_tokens:
            tool["max_content_tokens"] = max_content_tokens
        return tool

    @staticmethod
    def search_hits(blocks: list[Any]) -> list[WebSearchHit]:
        """The search results in a response, with the failure branch handled.

        A server-tool failure arrives as HTTP 200, not as an exception: the
        result block's `content` is a single error object instead of a list.
        Branch on that before indexing — it is the difference between "no
        results this run" and a TypeError inside the worker loop.
        """
        hits: list[WebSearchHit] = []
        for block in blocks:
            if getattr(block, "type", None) != "web_search_tool_result":
                continue
            content = getattr(block, "content", None)
            if not isinstance(content, list):
                logger.warning(
                    "web search failed",
                    extra={
                        "extra_fields": {"error_code": str(getattr(content, "error_code", content))}
                    },
                )
                continue
            for result in content:
                if getattr(result, "type", None) != "web_search_result":
                    continue
                url = str(getattr(result, "url", "") or "")
                if url:
                    hits.append(
                        WebSearchHit(
                            title=str(getattr(result, "title", "") or ""),
                            url=url,
                            page_age=getattr(result, "page_age", None),
                        )
                    )
        return hits

    @staticmethod
    def fetched_pages(blocks: list[Any]) -> list[FetchedPage]:
        """The pages the fetch tool actually retrieved, as plain text.

        The success shape is an object (`web_fetch_result`) wrapping a document
        block whose `source.data` is the text; a failure is an object of a
        different type carrying an `error_code`. Both arrive at HTTP 200, so
        the type is checked before anything is read out of it, and a page that
        could not be fetched simply does not appear in the result.

        A PDF comes back through the same shape with a **base64** source, and
        several allow-listed hosts (who.int, thelancet.com, lex.uz, stat.uz)
        serve them routinely. Returning that blob would hand the drafting call
        an unreadable string labelled as the text of the page — grounding in
        name only. The source type is therefore checked here, in code: a
        non-text document is dropped exactly like a failed fetch, so the
        caller's rule that a page which did not come through is never drafted
        holds without depending on the model noticing.
        """
        pages: list[FetchedPage] = []
        for block in blocks:
            if getattr(block, "type", None) != "web_fetch_tool_result":
                continue
            result = getattr(block, "content", None)
            if getattr(result, "type", None) != "web_fetch_result":
                logger.warning(
                    "web fetch failed",
                    extra={
                        "extra_fields": {"error_code": str(getattr(result, "error_code", result))}
                    },
                )
                continue
            document = getattr(result, "content", None)
            source = getattr(document, "source", None)
            url = str(getattr(result, "url", "") or "")

            source_type = str(getattr(source, "type", "") or "")
            media_type = str(getattr(source, "media_type", "") or "")
            if source_type != "text" or (media_type and not media_type.startswith("text/")):
                logger.info(
                    "web fetch returned a non-text document; dropping",
                    extra={
                        "extra_fields": {
                            "url": url,
                            "source_type": source_type,
                            "media_type": media_type,
                        }
                    },
                )
                continue

            text = str(getattr(source, "data", "") or "")
            if url and text:
                pages.append(
                    FetchedPage(
                        url=url,
                        title=str(getattr(document, "title", "") or ""),
                        text=text,
                    )
                )
        return pages

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
