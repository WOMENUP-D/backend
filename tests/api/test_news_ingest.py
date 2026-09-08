"""The unattended news job, end to end, without a network call.

This is the only path in the portal where a machine writes to the front page
with nobody reading it first, so what is pinned here is mostly what it must
*not* do: post the same article twice, publish something it was not allowed to
publish, write an article it did not read, or turn a failure of any kind into
an error a reader would see.

The gateway is replaced with a scripted fake, exactly as the news-editor tests
do it — `monkeypatch.setattr("app.services.news_ingest.llm_gateway", ...)`. The
fake answers two different calls: the scout call (tools, no schema) returns
search results and fetched page text as raw blocks, and each drafting call
(schema, no tools) returns one payload.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import Role
from app.core.security import create_token
from app.models.audit import AiInteraction, AuditLog
from app.models.news import NewsPost
from app.models.user import User
from app.services.llm_gateway import LlmUnavailableError
from app.services.news_ingest import ingest_news

WHO_URL = "https://www.who.int/news/item/2026-09-01-womens-health-report"
UZA_URL = "https://uza.uz/uz/posts/ayollar-uchun-yangi-dastur"

PAGE_TEXT = (
    "The World Health Organization published a report on women's health on 1 September 2026. "
    "It sets out what is known about screening coverage across the region and where the gaps "
    "are. The report recommends that women speak to a doctor about screening intervals."
)

CLEAN_BODY = (
    "Jahon sogʻliqni saqlash tashkiloti ayollar salomatligi boʻyicha yangi hisobot eʼlon qildi.\n\n"
    "Hisobotda skrining qamrovi va mintaqadagi farqlar haqida maʼlumot berilgan.\n\n"
    "Oʻzingizga tegishli tavsiyalar uchun shifokorga murojaat qiling."
)


# --- the fake -------------------------------------------------------------


def _reply(**overrides) -> SimpleNamespace:
    base = {
        "text": "",
        "trace_id": "trace-test",
        "model": "claude-test",
        "prompt_version": "v-test",
        "latency_ms": 12,
        "input_tokens": 100,
        "output_tokens": 50,
        "stop_reason": "end_turn",
        "refused": False,
        "parsed": None,
        "raw_blocks": [],
        "resumes": 1,
    }
    return SimpleNamespace(**{**base, **overrides})


def _blocks(hits: list[tuple[str, str]], pages: dict[str, str]) -> list:
    search = SimpleNamespace(
        type="web_search_tool_result",
        content=[
            SimpleNamespace(type="web_search_result", url=url, title=title, page_age="1 day ago")
            for url, title in hits
        ],
    )
    fetched = [
        SimpleNamespace(
            type="web_fetch_tool_result",
            content=SimpleNamespace(
                type="web_fetch_result",
                url=url,
                content=SimpleNamespace(
                    type="document",
                    title="A page",
                    source=SimpleNamespace(type="text", media_type="text/plain", data=text),
                ),
            ),
        )
        for url, text in pages.items()
    ]
    return [SimpleNamespace(type="text", text="I found these."), search, *fetched]


def _payload(url: str, **overrides) -> dict:
    base = {
        "source_url": url,
        "source_name": "World Health Organization",
        "category": "health",
        "title": {
            "uz": "JSST ayollar salomatligi boʻyicha hisobot eʼlon qildi",
            "ru": "ВОЗ опубликовала доклад о здоровье женщин",
            "en": "WHO publishes a report on women's health",
        },
        "summary": {"uz": "Yangi hisobot.", "ru": "Новый доклад.", "en": "A new report."},
        "body": {"uz": CLEAN_BODY, "ru": "Текст доклада.", "en": "Report text."},
        "tags": ["salomatlik", "jsst"],
        "reading_minutes": 4,
        "adult_only": False,
        "published_ago_days": 1,
        "rejected": False,
        "rejection_reason": "",
    }
    return {**base, **overrides}


class ScriptedGateway:
    """One scout turn, then one drafting reply per candidate."""

    enabled = True

    def __init__(self, *, hits, pages, drafts, scout=None):
        self.hits = hits
        self.pages = pages
        self.drafts = list(drafts)
        self.scout = scout
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("json_schema") is None:
            return self.scout or _reply(
                trace_id="trace-scout", raw_blocks=_blocks(self.hits, self.pages)
            )
        payload = self.drafts.pop(0) if self.drafts else None
        return _reply(trace_id="trace-draft", parsed=payload, refused=payload is None)


def _one_who_article(**payload_overrides) -> ScriptedGateway:
    return ScriptedGateway(
        hits=[(WHO_URL, "WHO publishes report")],
        pages={WHO_URL: PAGE_TEXT},
        drafts=[_payload(WHO_URL, **payload_overrides)],
    )


def _use(monkeypatch, gateway: ScriptedGateway) -> ScriptedGateway:
    monkeypatch.setattr("app.services.news_ingest.llm_gateway", gateway)
    return gateway


# --- fixtures -------------------------------------------------------------


async def _make_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = User(id=user_id, phone=f"+9989{user_id.int % 10**8:08d}")
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def editor_headers(session: AsyncSession) -> dict[str, str]:
    editor_id = uuid.uuid4()
    await _make_user(session, editor_id)
    token = create_token(editor_id, Role.ADMIN, "access", {"roles": [Role.ADMIN.value]})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def ingest_enabled(monkeypatch):
    monkeypatch.setattr(settings, "news_ingest_enabled", True)
    monkeypatch.setattr(settings, "news_ingest_auto_publish", True)


async def _posts(session: AsyncSession) -> list[NewsPost]:
    rows = await session.execute(select(NewsPost))
    return list(rows.scalars())


# --- grounding ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_candidate_whose_page_was_not_fetched_is_never_drafted(session, monkeypatch):
    """The rule that keeps a real WHO link off an invented paragraph. A search
    result is a title and a URL; if the page itself did not come back there is
    nothing to write the post from, so the candidate is dropped rather than
    written from its headline."""
    gateway = _use(
        monkeypatch,
        ScriptedGateway(
            hits=[(WHO_URL, "WHO publishes report")],
            pages={},  # nothing fetched
            drafts=[_payload(WHO_URL)],
        ),
    )

    result = await ingest_news(session)

    assert result.drafted == 0
    assert await _posts(session) == []
    # The drafting call was never made — which is also the cheap outcome.
    assert all(call.get("json_schema") is None for call in gateway.calls)


@pytest.mark.asyncio
async def test_the_fetched_page_text_is_what_the_drafting_call_is_given(session, monkeypatch):
    gateway = _use(monkeypatch, _one_who_article())

    await ingest_news(session)

    draft_call = next(call for call in gateway.calls if call.get("json_schema") is not None)
    assert PAGE_TEXT in draft_call["messages"][0]["content"]


@pytest.mark.asyncio
async def test_a_draft_attributed_to_a_url_it_was_not_given_is_discarded(session, monkeypatch):
    """The hallucinated-attribution case. The post would carry a genuine WHO
    link over text drawn from somewhere else entirely."""
    _use(
        monkeypatch,
        ScriptedGateway(
            hits=[(WHO_URL, "WHO publishes report")],
            pages={WHO_URL: PAGE_TEXT},
            drafts=[_payload(WHO_URL, source_url="https://who.int/news/item/something-else")],
        ),
    )

    result = await ingest_news(session)

    assert result.drafted == 0
    assert await _posts(session) == []


@pytest.mark.asyncio
async def test_a_candidate_that_trips_the_safety_filter_never_reaches_the_model(
    session, monkeypatch
):
    """Safety before the model, literally: the material that would become the
    drafting prompt is filtered first, so a story about violence never becomes
    a generative prompt."""
    gateway = _use(
        monkeypatch,
        ScriptedGateway(
            hits=[(WHO_URL, "A report")],
            pages={WHO_URL: "A study of domestic violence and abuse against women."},
            drafts=[_payload(WHO_URL)],
        ),
    )

    result = await ingest_news(session)

    assert result.drafted == 0
    assert all(call.get("json_schema") is None for call in gateway.calls)


# --- deduplication --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_same_article_found_twice_is_posted_once(session, monkeypatch):
    """The check that makes this safe to run three times a day for years."""
    _use(monkeypatch, _one_who_article())
    first = await ingest_news(session)

    _use(monkeypatch, _one_who_article())
    second = await ingest_news(session)

    assert first.drafted == 1
    assert second.drafted == 0
    assert second.skipped_duplicate == 1
    assert len(await _posts(session)) == 1


@pytest.mark.asyncio
async def test_the_same_story_at_another_url_is_caught_by_its_title(session, monkeypatch):
    session.add(
        NewsPost(
            slug="existing-story",
            category="health",
            title_i18n={"uz": "JSST ayollar salomatligi boʻyicha hisobot eʼlon qildi"},
            summary_i18n={},
            body_i18n={},
            is_published=True,
        )
    )
    await session.flush()

    _use(monkeypatch, _one_who_article())
    result = await ingest_news(session)

    assert result.skipped_duplicate == 1
    assert result.drafted == 0


@pytest.mark.asyncio
async def test_the_same_headline_from_outside_the_window_is_not_a_duplicate(session, monkeypatch):
    """The title check is fuzzy, so it has to be bounded: an anniversary piece
    forty days after the original is a different article."""
    session.add(
        NewsPost(
            slug="old-story",
            category="health",
            title_i18n={"uz": "JSST ayollar salomatligi boʻyicha hisobot eʼlon qildi"},
            summary_i18n={},
            body_i18n={},
            is_published=True,
            created_at=datetime.now(UTC) - timedelta(days=40),
        )
    )
    await session.flush()

    _use(monkeypatch, _one_who_article())
    result = await ingest_news(session)

    assert result.drafted == 1


# --- what reaches a reader ------------------------------------------------


@pytest.mark.asyncio
async def test_a_trusted_source_clearing_the_gate_is_published_and_appears_in_the_feed(
    client, session, monkeypatch
):
    _use(monkeypatch, _one_who_article())

    result = await ingest_news(session)
    post = (await _posts(session))[0]

    assert result.published == 1
    assert post.is_published is True
    assert post.published_at is not None
    assert post.ai_meta["ingest"]["blocked_by"] == []

    feed = (await client.get("/api/v1/news")).json()
    assert post.slug in [item["slug"] for item in feed["items"]]


@pytest.mark.asyncio
async def test_with_auto_publish_off_everything_lands_as_a_draft(
    client, session, monkeypatch, auth_headers, editor_headers
):
    """The switch turns the same job into a drafting aid. A draft is 404 for a
    reader and readable by an editor — the rule that already governs every
    other draft on the portal."""
    monkeypatch.setattr(settings, "news_ingest_auto_publish", False)
    _use(monkeypatch, _one_who_article())

    result = await ingest_news(session)
    post = (await _posts(session))[0]

    assert result.published == 0
    assert result.skipped_gate == 1
    assert post.is_published is False
    assert post.published_at is None
    assert post.ai_meta["ingest"]["blocked_by"] == ["auto_publish_disabled"]

    feed = (await client.get("/api/v1/news")).json()
    assert feed["items"] == []
    assert (await client.get(f"/api/v1/news/{post.slug}", headers=auth_headers)).status_code == 404
    assert (
        await client.get(f"/api/v1/news/{post.slug}", headers=editor_headers)
    ).status_code == 200


@pytest.mark.asyncio
async def test_an_untrusted_source_is_drafted_but_not_published(session, monkeypatch):
    """`telegram.me` is not on the allowlist and is not a trusted host, and no
    number the model writes changes that."""
    monkeypatch.setattr(settings, "news_ingest_allowed_domains", ["who.int", "telegram.me"])
    url = "https://telegram.me/health/1"
    _use(
        monkeypatch,
        ScriptedGateway(
            hits=[(url, "A claim")],
            pages={url: PAGE_TEXT},
            drafts=[_payload(url, source_name="Health channel")],
        ),
    )

    result = await ingest_news(session)
    post = (await _posts(session))[0]

    assert result.published == 0
    assert post.is_published is False
    assert "source_not_trusted" in post.ai_meta["ingest"]["blocked_by"]


@pytest.mark.asyncio
async def test_an_adult_subject_is_withheld_from_an_anonymous_reader(client, session, monkeypatch):
    """The model said `adult_only: false`; the text says otherwise, and the
    text wins. An unknown age is treated as "may be a minor"."""
    body = CLEAN_BODY + "\n\nHomiladorlik davrida skrining haqida maʼlumot."
    _use(monkeypatch, _one_who_article(body={"uz": body, "ru": "Текст", "en": "Body"}))

    await ingest_news(session)
    post = (await _posts(session))[0]

    assert post.is_adult_only is True
    assert post.ai_meta["ingest"]["adult_only_forced"] is True

    feed = (await client.get("/api/v1/news")).json()
    assert post.slug not in [item["slug"] for item in feed["items"]]


@pytest.mark.asyncio
async def test_an_announcement_is_drafted_and_waits_for_a_person(session, monkeypatch):
    _use(monkeypatch, _one_who_article(category="announcement"))

    await ingest_news(session)
    post = (await _posts(session))[0]

    assert post.is_published is False
    assert "category_needs_review" in post.ai_meta["ingest"]["blocked_by"]


# --- degrade, don't fail --------------------------------------------------


@pytest.mark.asyncio
async def test_with_no_api_key_the_run_is_a_no_op(client, session):
    """The suite-wide `disable_llm` fixture is in force here, which is the
    same state as a deployment with no key configured."""
    before = (await client.get("/api/v1/news")).json()

    result = await ingest_news(session)

    assert result.reason == "ai_disabled"
    assert await _posts(session) == []
    assert (await client.get("/api/v1/news")).json() == before


@pytest.mark.asyncio
async def test_the_switch_off_means_the_timer_does_nothing(session, monkeypatch):
    monkeypatch.setattr(settings, "news_ingest_enabled", False)
    _use(monkeypatch, _one_who_article())

    assert (await ingest_news(session)).reason == "disabled"
    assert await _posts(session) == []


@pytest.mark.asyncio
async def test_a_model_that_rate_limits_yields_zero_posts_rather_than_an_error(
    session, monkeypatch
):
    class FailingGateway:
        enabled = True

        async def complete(self, **_kwargs):
            raise LlmUnavailableError("model rate limit reached")

    monkeypatch.setattr("app.services.news_ingest.llm_gateway", FailingGateway())

    result = await ingest_news(session)

    assert result.reason == "model_unavailable"
    assert await _posts(session) == []


@pytest.mark.asyncio
async def test_one_unusable_draft_does_not_lose_the_rest_of_the_run(session, monkeypatch):
    _use(
        monkeypatch,
        ScriptedGateway(
            hits=[(WHO_URL, "WHO report"), (UZA_URL, "UzA article")],
            pages={WHO_URL: PAGE_TEXT, UZA_URL: PAGE_TEXT},
            # The first drafting call comes back unparsable.
            drafts=[None, _payload(UZA_URL, source_name="UzA")],
        ),
    )

    result = await ingest_news(session)

    assert result.drafted == 1
    assert [post.source_url for post in await _posts(session)] == [UZA_URL]


@pytest.mark.asyncio
async def test_a_model_that_declines_an_item_costs_nothing(session, monkeypatch):
    _use(monkeypatch, _one_who_article(rejected=True, rejection_reason="not news"))

    result = await ingest_news(session)

    assert result.drafted == 0
    assert await _posts(session) == []


# --- the trace ------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_ingested_post_carries_the_calls_that_made_it(session, monkeypatch):
    """Section 06: model version, prompt version and a response trace on every
    AI output. Two trace ids because there were two calls, and "which search
    found this" and "which call wrote this text" are different questions."""
    _use(monkeypatch, _one_who_article())

    await ingest_news(session)
    post = (await _posts(session))[0]
    trace = post.ai_meta["ingest"]

    assert trace["model"] == "claude-test"
    assert trace["prompt_version"] == "v-test"
    assert trace["trace_id"] == "trace-draft"
    assert trace["scout_trace_id"] == "trace-scout"
    assert trace["scout_resumes"] == 1
    assert trace["search_result_url"] == WHO_URL
    assert trace["blocked_by"] == []

    rows = await session.execute(
        select(AiInteraction).where(AiInteraction.feature == "news_ingest")
    )
    assert len(list(rows.scalars())) == 2


@pytest.mark.asyncio
async def test_pressing_analyse_afterwards_does_not_erase_where_the_article_came_from(
    client, session, monkeypatch, editor_headers
):
    """The regression test for `apply_analysis`'s wholesale `ai_meta` write.
    Without the merge, an editor pressing "Analyse" on an ingested post
    silently destroys its provenance — the opposite of what the trace
    requirement is for."""
    _use(monkeypatch, _one_who_article())
    await ingest_news(session)
    post = (await _posts(session))[0]

    response = await client.post(f"/api/v1/news/{post.id}/analyse", headers=editor_headers)
    await session.refresh(post)

    assert response.status_code == 200
    assert post.ai_meta["ingest"]["trace_id"] == "trace-draft"
    assert post.ai_meta["source"] == "rules"


# --- the manual trigger ---------------------------------------------------


@pytest.mark.asyncio
async def test_an_editor_can_run_it_now_and_the_audit_row_names_her(
    client, session, monkeypatch, editor_headers
):
    _use(monkeypatch, _one_who_article())

    response = await client.post("/api/v1/news/ingest?limit=1", headers=editor_headers)

    assert response.status_code == 200
    assert response.json()["published"] == 1

    rows = await session.execute(select(AuditLog).where(AuditLog.action == "news.ingest"))
    entry = rows.scalars().one()
    assert entry.actor_id is not None
    assert entry.changes["manual"] is True
    assert entry.changes["published"] == 1


@pytest.mark.asyncio
async def test_a_reader_cannot_run_the_ingest(client, auth_headers):
    response = await client.post("/api/v1/news/ingest", headers=auth_headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_the_manual_run_publishes_under_the_same_rules_as_the_timer(
    client, session, monkeypatch, editor_headers
):
    """A run *now*, not a run with fewer rules."""
    monkeypatch.setattr(settings, "news_ingest_auto_publish", False)
    _use(monkeypatch, _one_who_article())

    body = (await client.post("/api/v1/news/ingest", headers=editor_headers)).json()

    assert body["drafted"] == 1
    assert body["published"] == 0
