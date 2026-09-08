"""The gateway's server-tool surface: reading blocks, and resuming a paused turn.

Server tools fail differently from everything else in this codebase. A search
that hits its limit and a page that will not load both arrive as HTTP 200 with
an error object where a result should be, and a long tool turn ends with
`stop_reason: "pause_turn"` and no error at all. Each of those, unhandled, is a
silent wrong answer inside a background loop rather than an exception anybody
would see — so each is asserted here directly.

No network: a fake client is injected as `gateway._client`, which is the only
thing `complete` touches.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.llm_gateway import LlmGateway


def _search_result(url: str, title: str = "A title"):
    return SimpleNamespace(type="web_search_result", url=url, title=title, page_age="1 day ago")


def _fetch_block(url: str, text: str, title: str = "A page"):
    return SimpleNamespace(
        type="web_fetch_tool_result",
        content=SimpleNamespace(
            type="web_fetch_result",
            url=url,
            content=SimpleNamespace(
                type="document",
                title=title,
                source=SimpleNamespace(type="text", media_type="text/plain", data=text),
            ),
        ),
    )


def _message(*, content, stop_reason="end_turn", model="claude-test", tokens=(10, 5)):
    return SimpleNamespace(
        content=list(content),
        stop_reason=stop_reason,
        model=model,
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


class FakeMessages:
    """Records every request it is given and replays a scripted list."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[dict] = []

    async def create(self, **request):
        self.requests.append(request)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _gateway(replies) -> tuple[LlmGateway, FakeMessages]:
    gateway = LlmGateway(api_key="test-key", model="claude-test")
    messages = FakeMessages(replies)
    gateway._client = SimpleNamespace(messages=messages)
    return gateway, messages


# --- reading search results ----------------------------------------------


def test_search_results_come_back_as_plain_value_objects():
    blocks = [
        SimpleNamespace(type="text", text="I found two."),
        SimpleNamespace(
            type="web_search_tool_result",
            content=[_search_result("https://who.int/a"), _search_result("https://who.int/b")],
        ),
    ]

    hits = LlmGateway.search_hits(blocks)

    assert [hit.url for hit in hits] == ["https://who.int/a", "https://who.int/b"]
    assert hits[0].page_age == "1 day ago"


def test_a_failed_search_is_no_results_and_not_a_TypeError():
    """The one provider behaviour that would otherwise fail as a TypeError
    inside a background loop: on success `content` is a list, on failure it is
    a single error object. Branching on that is the whole point."""
    blocks = [
        SimpleNamespace(
            type="web_search_tool_result",
            content=SimpleNamespace(
                type="web_search_tool_result_error", error_code="max_uses_exceeded"
            ),
        )
    ]

    assert LlmGateway.search_hits(blocks) == []


# --- reading fetched pages -----------------------------------------------


def test_a_fetched_page_yields_its_text():
    """This is what makes a grounded draft possible. Without it the drafting
    call sees a headline and a URL, and writes the article from those."""
    pages = LlmGateway.fetched_pages([_fetch_block("https://who.int/a", "The full page text.")])

    assert len(pages) == 1
    assert pages[0].url == "https://who.int/a"
    assert pages[0].text == "The full page text."


def test_a_page_that_would_not_load_is_simply_absent():
    blocks = [
        SimpleNamespace(
            type="web_fetch_tool_result",
            content=SimpleNamespace(
                type="web_fetch_tool_result_error", error_code="url_not_accessible"
            ),
        )
    ]

    assert LlmGateway.fetched_pages(blocks) == []


def test_a_fetch_result_with_no_text_is_not_a_page():
    blocks = [_fetch_block("https://who.int/a", "")]
    assert LlmGateway.fetched_pages(blocks) == []


def test_a_pdf_is_dropped_rather_than_passed_off_as_page_text():
    """A PDF comes back through the success shape with a base64 source.

    who.int, thelancet.com, lex.uz and stat.uz all serve them routinely, and
    handing that blob to the drafting call would be grounding in name only —
    the model would receive a URL, a headline and an unreadable string labelled
    as the text of the page. Dropping it in code keeps the caller's rule ("a
    page that did not come through is never drafted") a boundary rather than a
    request the model is trusted to honour.
    """
    blocks = [
        SimpleNamespace(
            type="web_fetch_tool_result",
            content=SimpleNamespace(
                type="web_fetch_result",
                url="https://who.int/report.pdf",
                content=SimpleNamespace(
                    type="document",
                    title="Report",
                    source=SimpleNamespace(
                        type="base64", media_type="application/pdf", data="JVBERi0xLjQKJcfs"
                    ),
                ),
            ),
        )
    ]

    assert LlmGateway.fetched_pages(blocks) == []


# --- pause_turn ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_paused_turn_is_resumed_and_the_whole_turn_is_returned():
    """A long server-tool turn stops at HTTP 200 with `pause_turn`. Without
    resumption the caller gets a silently truncated answer and no error."""
    gateway, fake = _gateway(
        [
            _message(content=[SimpleNamespace(type="text", text="one ")], stop_reason="pause_turn"),
            _message(content=[SimpleNamespace(type="text", text="two ")], stop_reason="pause_turn"),
            _message(content=[SimpleNamespace(type="text", text="three")]),
        ]
    )

    response = await gateway.complete(
        system="s", messages=[{"role": "user", "content": "q"}], max_resumes=5
    )

    assert response.resumes == 2
    assert response.text == "one two three"
    assert response.stop_reason == "end_turn"
    # Tokens summed across the legs, so a paused turn is not reported cheaper
    # than it was.
    assert response.input_tokens == 30
    assert response.output_tokens == 15


@pytest.mark.asyncio
async def test_a_resumed_request_appends_the_assistant_turn_and_nothing_else():
    """No "Continue." message: the API resumes off the trailing
    `server_tool_use` block, and an extra user turn confuses it."""
    paused = _message(content=[SimpleNamespace(type="text", text="x")], stop_reason="pause_turn")
    gateway, fake = _gateway([paused, _message(content=[SimpleNamespace(type="text", text="y")])])

    await gateway.complete(system="s", messages=[{"role": "user", "content": "q"}], max_resumes=1)

    second = fake.requests[1]["messages"]
    assert [turn["role"] for turn in second] == ["user", "assistant"]
    assert second[1]["content"] == paused.content


@pytest.mark.asyncio
async def test_without_a_resume_budget_nothing_changes_for_existing_callers():
    """`max_resumes` defaults to 0, so every call site that predates server
    tools makes exactly one request and behaves as it always did."""
    gateway, fake = _gateway(
        [_message(content=[SimpleNamespace(type="text", text="x")], stop_reason="pause_turn")]
    )

    response = await gateway.complete(system="s", messages=[{"role": "user", "content": "q"}])

    assert len(fake.requests) == 1
    assert response.resumes == 0
    assert response.stop_reason == "pause_turn"


# --- request shaping -----------------------------------------------------


@pytest.mark.asyncio
async def test_tools_reach_the_request_and_structured_output_still_works():
    """Server tools and `json_schema` are not mutually exclusive in general —
    only citations are — so this combination must not be rejected."""
    gateway, fake = _gateway([_message(content=[SimpleNamespace(type="text", text='{"a": 1}')])])

    response = await gateway.complete(
        system="s",
        messages=[{"role": "user", "content": "q"}],
        tools=[{"type": "custom", "name": "t"}],
        json_schema={"type": "object"},
    )

    assert fake.requests[0]["tools"] == [{"type": "custom", "name": "t"}]
    assert response.parsed == {"a": 1}


@pytest.mark.asyncio
async def test_a_citation_producing_tool_with_a_schema_fails_before_any_call():
    """The documented incompatibility is between citation metadata and
    `output_config.format`. Failing here, with the reason attached, is the
    difference between a one-line fix and an afternoon — and it is scoped to
    citations so it can never block a legitimate tool call."""
    gateway, fake = _gateway([_message(content=[])])

    with pytest.raises(ValueError, match="citations"):
        await gateway.complete(
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[LlmGateway.web_fetch_tool() | {"citations": {"enabled": True}}],
            json_schema={"type": "object"},
        )

    assert fake.requests == []


def test_the_two_web_tools_are_pinned_to_the_same_generation():
    """Both `_20260209`, which carry dynamic filtering themselves — `code_execution`
    must never be declared beside them."""
    search = LlmGateway.web_search_tool(allowed_domains=["who.int"])
    fetch = LlmGateway.web_fetch_tool(allowed_domains=["who.int"], max_content_tokens=100)

    assert search["type"] == "web_search_20260209"
    assert fetch["type"] == "web_fetch_20260209"
    assert search["allowed_domains"] == fetch["allowed_domains"] == ["who.int"]
    assert "blocked_domains" not in search and "blocked_domains" not in fetch
    assert fetch["max_content_tokens"] == 100
