"""The incremental reader that shows the `answer` field while it is written,
and the gateway's streamed round trip."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.answer_stream import AnswerFieldStream
from app.services.llm_gateway import LlmGateway, LlmUnavailableError


def _read(raw: str, size: int) -> str:
    reader = AnswerFieldStream()
    return "".join(reader.feed(raw[i : i + size]) for i in range(0, len(raw), size))


PAYLOAD = {
    "kind": "general",
    "nested": {"answer": "not this one", "list": ["a", {"answer": "nor this"}]},
    "answer": 'Salom, "dunyo"!\nYo\'l: C:\\dir / \u00e9 \u2014 \U0001f600 tab\tend',
    "next_actions": ["x", "y"],
}


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 64, 10_000])
@pytest.mark.parametrize("ascii_only", [True, False])
def test_chunked_anywhere_it_decodes_exactly_the_answer(size, ascii_only):
    raw = json.dumps(PAYLOAD, ensure_ascii=ascii_only)
    assert _read(raw, size) == PAYLOAD["answer"]


def test_answer_first_as_the_schemas_write_it():
    raw = json.dumps({"answer": "Birinçi", "kind": "general", "next_actions": []}, indent=2)
    assert _read(raw, 3) == "Birinçi"


def test_text_arrives_before_the_string_closes():
    reader = AnswerFieldStream()
    assert reader.feed('{"answer": "Sal') == "Sal"
    assert reader.feed("om \\u00") == "om "
    assert reader.feed("e9!") == "\u00e9!"
    assert reader.feed('", "kind": "x"}') == ""
    assert reader.done


def test_a_split_surrogate_pair_is_joined():
    reader = AnswerFieldStream()
    out = reader.feed('{"answer": "\\ud83d') + reader.feed('\\ude00"}')
    assert out == "\U0001f600"


def test_no_answer_key_yields_nothing():
    assert _read(json.dumps({"route": "health"}), 4) == ""


def test_an_empty_answer_is_done():
    reader = AnswerFieldStream()
    assert reader.feed('{"answer": "", "kind": "general"}') == ""
    assert reader.done


# ------------------------------------------------------------ the gateway


def _event(text: str):
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text)
    )


class _FakeStream:
    def __init__(self, events, final, delay: float = 0.0, fail_at: int | None = None):
        self.events, self.final, self.delay, self.fail_at = events, final, delay, fail_at

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for index, event in enumerate(self.events):
            if self.fail_at == index:
                import anthropic
                import httpx

                raise anthropic.APIConnectionError(request=httpx.Request("POST", "http://x"))
            if self.delay:
                await asyncio.sleep(self.delay)
            yield event

    async def get_final_message(self):
        return self.final


def _gateway(stream: _FakeStream, calls: list) -> LlmGateway:
    gateway = LlmGateway(api_key="test-key", model="test-model")

    def open_stream(**request):
        calls.append(request)
        return stream

    gateway._client = SimpleNamespace(
        messages=SimpleNamespace(stream=open_stream, create=None)  # type: ignore[arg-type]
    )
    return gateway


def _final(text: str):
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        stop_reason="end_turn",
        model="test-model",
    )


@pytest.mark.asyncio
async def test_the_streamed_call_forwards_text_and_returns_the_same_response():
    raw = json.dumps({"answer": "Salom", "kind": "general", "next_actions": []})
    pieces = [raw[:5], raw[5:13], raw[13:]]
    events = [SimpleNamespace(type="message_start")] + [_event(p) for p in pieces]
    calls: list = []
    gateway = _gateway(_FakeStream(events, _final(raw)), calls)
    seen: list[str] = []

    async def on_text(text: str) -> None:
        seen.append(text)

    response = await gateway.complete(
        system="s",
        messages=[{"role": "user", "content": "q"}],
        json_schema={"type": "object"},
        timeout=5,
        on_text=on_text,
    )
    assert seen == pieces
    assert response.parsed == json.loads(raw)
    assert response.text == raw
    assert (response.input_tokens, response.output_tokens) == (11, 7)
    assert response.model == "test-model" and response.prompt_version
    # The very request a plain call makes — model, effort and all.
    assert calls[0]["output_config"]["format"]["type"] == "json_schema"
    assert calls[0]["thinking"] == {"type": "adaptive"}
    assert calls[0]["timeout"] == 5


@pytest.mark.asyncio
async def test_a_stream_that_outlasts_the_timeout_degrades():
    gateway = _gateway(_FakeStream([_event("x")] * 50, _final("x"), delay=0.02), [])

    async def on_text(text: str) -> None:
        return None

    with pytest.raises(LlmUnavailableError):
        await gateway.complete(system="s", messages=[], timeout=0.1, on_text=on_text)


@pytest.mark.asyncio
async def test_a_broken_stream_degrades():
    gateway = _gateway(_FakeStream([_event("a"), _event("b")], _final("ab"), fail_at=1), [])
    seen: list[str] = []

    async def on_text(text: str) -> None:
        seen.append(text)

    with pytest.raises(LlmUnavailableError):
        await gateway.complete(system="s", messages=[], on_text=on_text)
    assert seen == ["a"]
