"""AI news ingestion: the model searches and drafts, the code decides.

Every eight hours the worker asks the model to look for articles worth
carrying on the portal's front page, and — for anything that clears the gate
below — publishes them without a human in the loop. Three rules shape the
whole module, and each of them exists because the alternative is a real
failure a reader would see.

* **Grounded, or dropped.** A search result is a title and a link, and a title
  and a link are enough to invent an article from. So the scout call fetches
  the page as well as finding it, and a candidate whose page could not be
  fetched is discarded rather than drafted. A fabricated paragraph carrying a
  genuine WHO link is precisely the failure `source_name` exists to prevent.
* **The code decides what is published.** `publication_gate` is a list of
  facts about the row — attribution, the source host, the escalation keyword
  filter, the vocabulary of a verdict — none of which is a request to the
  model. `age_gate` states the distinction in its own docstring: a prompt
  instruction is a request; this is the boundary. In particular the gate never
  reads `credibility_score`, because the model writes that column.
* **Degrade to zero posts, never to an error.** No API key, a rate limit, a
  refusal, an unparsable reply or a page that would not load all mean the feed
  is unchanged this run. Nothing here raises into the worker loop.

What this is not: a guarantee. `VERDICT_MARKERS` is keyword matching across
three languages and will both miss and over-trigger, exactly as
`age_gate.ADULT_HEALTH_TOPICS` and `rag.ESCALATION_KEYWORDS` already do.
Over-triggering costs a post that waits for an editor. Missing costs more. It
is a floor under the prompt, not a substitute for one.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import NewsCategory
from app.models.audit import AiInteraction
from app.models.news import NewsPost
from app.schemas.news import COVER_TONES
from app.services import rag
from app.services.age_gate import ADULT_HEALTH_TOPICS, fold
from app.services.llm_gateway import (
    FetchedPage,
    LlmGateway,
    LlmResponse,
    LlmUnavailableError,
    WebSearchHit,
    llm_gateway,
)
from app.services.news_age import searchable_text
from app.services.news_ai import Analysis, analyse, apply_analysis
from app.services.news_ranking import TRUSTED_SOURCES
from app.services.prompts import (
    NEWS_DRAFT_SCHEMA,
    NEWS_DRAFT_SYSTEM,
    NEWS_SCOUT_SYSTEM,
)

logger = logging.getLogger(__name__)

# The audit action every run writes, scheduled or manual. One string, shared,
# because the worker reads the latest row with this action back as its schedule
# marker — a second spelling would silently mean "never ran".
AUDIT_ACTION = "news.ingest"

# Categories an unattended job may publish into. ANNOUNCEMENT is absent on
# purpose: a deadline nobody verified is the worst thing this feed could carry,
# and the AI has no way to verify one. Announcements still get drafted — they
# just wait for a person.
SAFE_CATEGORIES: frozenset[NewsCategory] = frozenset(
    {
        NewsCategory.HEALTH,
        NewsCategory.MEDICINE,
        NewsCategory.SCIENCE,
        NewsCategory.EDUCATION,
        NewsCategory.CAREER,
        NewsCategory.SUCCESS_STORY,
    }
)

# Hosts whose name alone is an argument: the URL half of
# `news_ranking.TRUSTED_SOURCES`, plus the national sources a portal for women
# in Uzbekistan treats the same way. Matched by `_host_matches`, so
# `evil-who.int` and `who.int.example.com` are not WHO.
#
# A code constant rather than a setting, and narrower than
# `settings.news_ingest_allowed_domains`: "which sites may be searched" and
# "which sites may reach a reader unread" are different decisions, and only the
# first one is an operator's to widen from a .env file.
TRUSTED_DOMAINS: tuple[str, ...] = (
    "uza.uz",
    "gov.uz",
    "ssv.uz",
    "minzdrav.uz",
    "stat.uz",
    "lex.uz",
    "who.int",
    "un.org",
    "unwomen.org",
    "unicef.org",
    "unesco.org",
    "unfpa.org",
    "nobelprize.org",
    "thelancet.com",
    "nature.com",
    "science.org",
    "bmj.com",
    "nejm.org",
    "cochrane.org",
    "cdc.gov",
)

# Vocabulary of a verdict rather than of information. A post carrying any of
# these is queued for a human whatever else it clears.
#
# Two Uzbek entries — "iching" (drink) and "qabul qiling" (take/accept) — are
# ordinary words that will fire on copy which is not a verdict at all. They
# stay: a false positive costs a post that waits for an editor, a miss costs a
# dose instruction on a national portal. The tests say so out loud rather than
# pretending these are precise.
VERDICT_MARKERS: tuple[str, ...] = (
    # --- uz ---
    "qabul qiling",
    "iching",
    "davolaydi",
    "dori iching",
    "mg kuniga",
    "kuniga mg",
    "tashxis qoying",
    "shifo topasiz",
    # --- ru ---
    "принимайте",
    "выпейте",
    "назначьте",
    "вылечивает",
    "излечивает",
    "дозировка",
    "мг в день",
    "мг в сутки",
    "поставьте диагноз",
    "гарантирует излечение",
    # --- en ---
    "prescribe",
    "mg per day",
    "mg a day",
    "mg daily",
    "guaranteed cure",
    "self-medicate",
    "diagnose yourself",
    "dosage",
)

# Uzbek and Russian Cyrillic to Latin, for slugs only. Not a transliteration
# anyone should read: it exists so that a title written entirely in Cyrillic
# still produces a slug with words in it instead of eight hex characters.
_CYRILLIC_TO_LATIN: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d", "е": "e",
    "ё": "yo", "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "қ": "q",
    "л": "l", "м": "m", "н": "n", "о": "o", "ў": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ҳ": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "i", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}  # fmt: skip

# The slug is `<stem>-<8 hex>`, so the stem is capped well below the column's
# 120 characters and the whole thing can never exceed 109.
SLUG_STEM_MAX = 100

# Two titles this close are the same story under two headlines.
TITLE_SIMILARITY = 0.86

# How much of a fetched page is handed to the drafting call. A news article is
# comfortably inside this; the cap is here so one enormous page cannot turn a
# routine run into an expensive one.
DRAFT_CONTEXT_CHARS = 24_000


@dataclass(slots=True)
class IngestResult:
    """What one run did. Counts, plus why it did nothing when it did nothing."""

    considered: int = 0
    drafted: int = 0
    published: int = 0
    skipped_duplicate: int = 0
    skipped_gate: int = 0
    reason: str | None = None


# --- URLs ----------------------------------------------------------------


def canonical_url(url: str) -> str:
    """Host and path, lowercased, with the noise removed.

    The scheme is dropped rather than lowercased: the same article served over
    http and over https is the same article, and re-posting it the day a site
    moved to TLS would be a bug with a story attached. Query and fragment go
    too — they carry campaign tags, not identity.

    A URL with no host at all canonicalises to the empty string, which every
    caller below reads as "not usable".
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return ""
    return f"{host}{parts.path.rstrip('/')}"


def url_hash(url: str) -> str:
    """The deduplication key: sha256 of the canonical form."""
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()


def _host(url: str) -> str:
    return canonical_url(url).split("/", 1)[0]


def _host_matches(host: str, domains: Iterable[str]) -> bool:
    """Exact host, or a subdomain of one — never a suffix of the label.

    The leading dot is the whole point: a plain `endswith` would read
    `evil-who.int` as WHO, and `who.int.example.com` is not WHO either.
    """
    if not host:
        return False
    for domain in domains:
        candidate = domain.strip().lower().removeprefix("www.")
        if candidate and (host == candidate or host.endswith(f".{candidate}")):
            return True
    return False


def domain_allowed(url: str) -> bool:
    """Whether this URL is on the operator's search allowlist."""
    return _host_matches(_host(url), settings.news_ingest_allowed_domains)


def source_is_trusted(post: NewsPost) -> bool:
    """Whether the source behind this post may reach a reader unread.

    Derived from the source, never from a score on the row. This deliberately
    does NOT call `news_ranking.credibility_of`: that helper short-circuits on
    `post.credibility_score`, and `news_ai.apply_analysis` writes that column
    from the model's own reply on every model-backed run. Gating on it would
    let a draft certify itself by returning `credibility: 95`, which is not a
    trust decision at all.

    The host is the fact the model cannot forge — `build_post` copies it from
    the search result, never from the drafted payload. The name is model-
    written, so it can only ever corroborate a host the operator has already
    allow-listed; it can never confer trust on one they have not.
    """
    host = _host(post.source_url or "")
    if _host_matches(host, TRUSTED_DOMAINS):
        return True
    name = fold(post.source_name or "")
    return (
        bool(name)
        and domain_allowed(post.source_url or "")
        and any(trusted in name for trusted in TRUSTED_SOURCES)
    )


# --- text ----------------------------------------------------------------


def _slugify(text: str) -> str:
    """A URL-safe stem, in Latin letters, from a title in any of our scripts.

    There is no slugify in this repository and the frontend's transliteration
    runs the other way (Latin to Cyrillic), so the Cyrillic pass above is
    needed: without it a Russian-only headline would reduce to nothing and
    every such post would get a slug made only of hex.
    """
    latin = "".join(_CYRILLIC_TO_LATIN.get(char, char) for char in fold(text))
    stem = re.sub(r"[^a-z0-9]+", "-", latin).strip("-")
    return stem[:SLUG_STEM_MAX].strip("-")


def build_slug(title: str, digest: str) -> str:
    """`<words>-<8 hex>` — always `^[a-z0-9-]+$`, always at most 109 characters.

    Ending in the URL hash makes slug collisions impossible by construction
    rather than by retry: two different articles with the same headline get
    different slugs, and the same article always gets the same one.
    """
    return f"{_slugify(title) or 'yangilik'}-{digest[:8]}"


def _body_text(post: NewsPost) -> str:
    """Every locale of the body in one string, for keyword matching."""
    return " ".join(str(value) for value in (post.body_i18n or {}).values() if value)


def _post_text(post: NewsPost) -> str:
    """Everything on a post worth running a keyword filter over."""
    head = searchable_text(post.title_i18n, post.summary_i18n, list(post.tags or []))
    return f"{head} {_body_text(post)}"


def force_adult_only(post: NewsPost) -> bool:
    """Whether this post belongs to adult care whatever the model said.

    Applied as `payload_flag or force_adult_only(post)`: the model may raise
    the flag, never lower it. Not a publishing question — it applies to a draft
    and to a published post alike, and `api.news` already withholds such a post
    from a minor and from a reader whose age is unknown.
    """
    folded = fold(_post_text(post))
    return any(topic in folded for topic in ADULT_HEALTH_TOPICS)


# --- the gate ------------------------------------------------------------


def publication_gate(post: NewsPost, analysis: Analysis) -> list[str]:
    """Why this post may not be published without a human. Empty means it may.

    Every clause is a fact about the row, checkable offline, and none of it is
    a request to the model. The result is stored on the post whether it passed
    or not, so a moderator can see exactly which clause stopped it.
    """
    blocked: list[str] = []
    text = _post_text(post)
    folded = fold(text)

    if not settings.news_ingest_auto_publish:
        blocked.append("auto_publish_disabled")
    if not (post.source_name or "").strip() or not (post.source_url or "").strip():
        blocked.append("unattributed")
    if not domain_allowed(post.source_url or ""):
        blocked.append("source_domain_not_allowed")
    if not source_is_trusted(post):
        blocked.append("source_not_trusted")
    if post.category not in SAFE_CATEGORIES:
        blocked.append("category_needs_review")
    if not (post.title_i18n or {}).get("uz") or not (post.body_i18n or {}).get("uz"):
        # Uzbek is the primary locale. A post the portal cannot show in it is
        # not finished, whatever else it got right.
        blocked.append("missing_primary_locale")
    if rag.needs_human_escalation(text):
        blocked.append("safety_topic")
    if any(marker in folded for marker in VERDICT_MARKERS):
        blocked.append("reads_as_a_verdict")
    # One-way only: the analysing model can block its own post by scoring the
    # source low, and can never clear one by scoring it high — that is what
    # `source_is_trusted` is for.
    if analysis.source == "model" and (analysis.credibility or 0) < 70:
        blocked.append("editor_scored_credibility_low")
    return blocked


# --- the two model calls -------------------------------------------------


def _scout_brief() -> str:
    now = datetime.now(UTC)
    return "\n".join(
        [
            f"Today is {now.date().isoformat()} (UTC).",
            f"Look for articles published within the last "
            f"{settings.news_ingest_lookback_hours} hours.",
            f"Report at most {settings.news_ingest_max_posts} articles.",
            "",
            "Search, then fetch each article you intend to report, then "
            "describe what the fetched pages actually say.",
        ]
    )


async def _scout(brief: str) -> LlmResponse:
    """Call one: find candidate articles and read them.

    Search and fetch ride together so the pages themselves land in the
    conversation. `max_resumes` is what keeps a long tool turn from being
    returned as a silently truncated answer.
    """
    domains = list(settings.news_ingest_allowed_domains)
    return await llm_gateway.complete(
        system=NEWS_SCOUT_SYSTEM,
        messages=[{"role": "user", "content": brief}],
        tools=[
            LlmGateway.web_search_tool(max_uses=6, allowed_domains=domains),
            LlmGateway.web_fetch_tool(
                max_uses=8, allowed_domains=domains, max_content_tokens=20_000
            ),
        ],
        max_resumes=5,
    )


async def _draft(hit: WebSearchHit, page: FetchedPage) -> LlmResponse | None:
    """Call two: one fetched page in, one structured post out.

    A separate call because the fetched document carries citation metadata and
    structured output cannot ride alongside citations. The page's own text goes
    in as CONTEXT, which is what makes this a summary rather than an invention.
    """
    message = "\n".join(
        [
            f"URL: {hit.url}",
            f"Search result title: {hit.title or page.title}",
            f"Page title: {page.title}",
            f"Page age reported by the search tool: {hit.page_age or 'unknown'}",
            "Categories you may choose from: "
            + ", ".join(category.value for category in NewsCategory),
            "",
            "CONTEXT — the fetched text of that page. Everything you write must",
            "be supported by it:",
            page.text[:DRAFT_CONTEXT_CHARS],
        ]
    )
    try:
        response = await llm_gateway.complete(
            system=NEWS_DRAFT_SYSTEM,
            messages=[{"role": "user", "content": message}],
            json_schema=NEWS_DRAFT_SCHEMA,
        )
    except LlmUnavailableError as exc:
        logger.warning("news ingest: draft unavailable: %s", exc)
        return None
    if response.refused or not response.parsed:
        logger.info(
            "news ingest: no usable draft",
            extra={"extra_fields": {"trace_id": response.trace_id, "url": hit.url}},
        )
        return None
    return response


# --- building the row ----------------------------------------------------


def _tri(payload: dict[str, Any], key: str) -> dict[str, str] | None:
    """One translated field, with uz required and the others filled from it.

    A missing Russian title is a gap; a missing Uzbek one means the post cannot
    be shown in the portal's primary language, so the whole draft is refused.
    """
    raw = payload.get(key)
    if not isinstance(raw, dict):
        return None
    uz = str(raw.get("uz") or "").strip()
    if not uz:
        return None
    return {lang: str(raw.get(lang) or "").strip() or uz for lang in ("uz", "ru", "en")}


def _tags(payload: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for value in payload.get("tags") or []:
        tag = str(value).strip().lower()[:60]
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:8]


def _reading_minutes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 3
    return max(1, min(90, int(value)))


def build_post(payload: dict[str, Any], hit: WebSearchHit) -> NewsPost | None:
    """A row from a drafted payload, with every judgement re-decided here.

    Returns None when the draft is unusable — the model declined it, it lost
    the primary locale, it is too old to be news, or it attributed itself to a
    URL other than the one it was given. That last case is the one worth
    naming: a drafted `source_url` that disagrees with the search result is a
    hallucinated attribution, and it is dropped rather than corrected.
    """
    if payload.get("rejected"):
        logger.info(
            "news ingest: model declined an item: %s",
            str(payload.get("rejection_reason") or "")[:200],
        )
        return None

    digest = url_hash(hit.url)
    if not canonical_url(hit.url):
        return None
    if canonical_url(str(payload.get("source_url") or "")) != canonical_url(hit.url):
        logger.warning("news ingest: drafted URL does not match the search result; dropped")
        return None

    try:
        category = NewsCategory(str(payload.get("category")))
    except ValueError:
        logger.info("news ingest: unknown category %r; dropped", payload.get("category"))
        return None

    title = _tri(payload, "title")
    summary = _tri(payload, "summary")
    body = _tri(payload, "body")
    if title is None or summary is None or body is None:
        return None

    age_days = payload.get("published_ago_days")
    stale_after_days = max(1, settings.news_ingest_lookback_hours * 3 // 24)
    if isinstance(age_days, int) and not isinstance(age_days, bool) and age_days > stale_after_days:
        logger.info("news ingest: %s is too old to be news; dropped", hit.url)
        return None

    source_name = str(payload.get("source_name") or "").strip()[:160]

    return NewsPost(
        slug=build_slug(title["uz"], digest),
        category=category,
        title_i18n=title,
        summary_i18n=summary,
        body_i18n=body,
        # Never a hotlink: `models.news` is explicit that a cover must be
        # served from our own storage, and this job has nowhere to put one.
        # The drawn fallback is picked deterministically so re-ingesting the
        # same URL would look the same.
        cover_url=None,
        cover_tone=COVER_TONES[int(digest[:2], 16) % len(COVER_TONES)],
        source_name=source_name,
        source_url=hit.url[:500],
        source_url_hash=digest,
        tags=_tags(payload),
        reading_minutes=_reading_minutes(payload.get("reading_minutes")),
    )


# --- deduplication -------------------------------------------------------


async def _url_is_known(session: AsyncSession, digest: str) -> bool:
    """Exact source, forever. Index-backed, no time window.

    This is the check that makes the job safe to run three times a day
    indefinitely, and `ix_news_posts_source_url_hash` backs it, so even a race
    between two workers ends in an IntegrityError rather than a second card.
    """
    return bool(await session.scalar(select(NewsPost.id).where(NewsPost.source_url_hash == digest)))


async def _recent_titles(session: AsyncSession) -> list[dict]:
    """Titles from the dedup window, read once per run.

    Bounded by design: at six posts a run, three runs a day, thirty days is a
    few hundred rows of one JSONB column — one page, compared in Python, well
    inside a worker tick.
    """
    cutoff = datetime.now(UTC) - timedelta(days=settings.news_ingest_dedup_days)
    rows = await session.execute(select(NewsPost.title_i18n).where(NewsPost.created_at >= cutoff))
    return [title or {} for (title,) in rows]


def is_near_duplicate(title_i18n: dict, recent: list[dict]) -> bool:
    """The same story syndicated under a different URL and a different headline.

    All three locales are compared because the same story arrives titled in
    whichever language the source publishes in, and `fold` is reused so Uzbek
    apostrophe variants compare equal.
    """
    candidate = fold(str(title_i18n.get("uz") or ""))
    if not candidate:
        return False
    for other_i18n in recent:
        for lang in ("uz", "ru", "en"):
            other = fold(str((other_i18n or {}).get(lang) or ""))
            if other and SequenceMatcher(None, candidate, other).ratio() >= TITLE_SIMILARITY:
                return True
    return False


# --- traces --------------------------------------------------------------


def _interaction(
    response: LlmResponse, *, prompt: str, sources: list[str], refused: bool = False
) -> AiInteraction:
    """One model call, recorded. Excerpts are PII-scrubbed and truncated."""
    return AiInteraction(
        user_id=None,
        feature="news_ingest",
        model_version=response.model,
        prompt_version=response.prompt_version,
        trace_id=response.trace_id,
        prompt_excerpt=LlmGateway.sanitise(prompt)[:1000],
        response_excerpt=LlmGateway.sanitise(response.text)[:1000],
        retrieved_sources=sources,
        refused=refused or response.refused,
        latency_ms=response.latency_ms,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )


# --- the run -------------------------------------------------------------


async def ingest_news(
    session: AsyncSession,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> IngestResult:
    """One ingestion run. Never raises.

    `force` bypasses `news_ingest_enabled` for a human who has explicitly asked
    for a run now; it does not bypass `news_ingest_auto_publish`, so running it
    by hand cannot publish anything the timer would not have.

    `dry_run` builds and gates everything and adds no rows at all, which is how
    the publication gate is reviewable before it is trusted.
    """
    result = IngestResult()

    if not settings.news_ingest_enabled and not force:
        result.reason = "disabled"
        return result
    if not llm_gateway.enabled:
        result.reason = "ai_disabled"
        return result

    brief = _scout_brief()
    try:
        scout = await _scout(brief)
    except LlmUnavailableError as exc:
        logger.warning("news ingest: scout unavailable: %s", exc)
        result.reason = "model_unavailable"
        return result
    except Exception:
        logger.exception("news ingest: the search call failed; the feed is unchanged")
        result.reason = "error"
        return result

    hits = LlmGateway.search_hits(scout.raw_blocks)
    pages = {canonical_url(page.url): page for page in LlmGateway.fetched_pages(scout.raw_blocks)}
    if not dry_run:
        session.add(_interaction(scout, prompt=brief, sources=[hit.url for hit in hits]))

    if scout.refused:
        result.reason = "refused"
        return result
    if not hits:
        result.reason = "no_results"
        return result

    cap = max(1, limit or settings.news_ingest_max_posts)
    recent = await _recent_titles(session)
    seen: set[str] = set()

    for hit in hits:
        if result.drafted >= cap:
            break
        result.considered += 1

        if not domain_allowed(hit.url):
            continue

        page = pages.get(canonical_url(hit.url))
        if page is None:
            # Nothing to write the post FROM. Drafting from a title and a URL
            # is how a real WHO link ends up attached to an invented
            # paragraph, so the candidate is dropped instead.
            logger.info("news ingest: %s was not fetched; dropped", hit.url)
            continue

        # Safety before the model, literally. The material that would become
        # the drafting prompt is filtered first — the same keyword filter the
        # navigator runs before retrieval — so a story about violence never
        # becomes a generative prompt.
        if rag.needs_human_escalation(f"{hit.title}\n{page.title}\n{page.text}"):
            logger.info("news ingest: candidate trips the escalation filter; dropped")
            continue

        digest = url_hash(hit.url)
        if digest in seen or await _url_is_known(session, digest):
            result.skipped_duplicate += 1
            continue

        response = await _draft(hit, page)
        if response is None or response.parsed is None:
            continue
        post = build_post(response.parsed, hit)
        if post is None:
            continue

        if is_near_duplicate(post.title_i18n, recent):
            result.skipped_duplicate += 1
            seen.add(digest)
            continue

        analysis = await analyse(post)
        apply_analysis(post, analysis)

        post.is_adult_only = bool(response.parsed.get("adult_only")) or force_adult_only(post)
        blocked = publication_gate(post, analysis)
        post.is_published = not blocked
        if post.is_published:
            post.published_at = datetime.now(UTC)

        # Merged under its own key, not assigned: `apply_analysis` replaces
        # `ai_meta` wholesale, so the provenance has to be written after it and
        # nested, or a later editor pressing "Analyse" would erase where the
        # article came from.
        post.ai_meta = {
            **(post.ai_meta or {}),
            "ingest": {
                "feature": "news_ingest",
                "model": response.model,
                "prompt_version": response.prompt_version,
                "trace_id": response.trace_id,
                "scout_trace_id": scout.trace_id,
                "scout_resumes": scout.resumes,
                "ingested_at": datetime.now(UTC).isoformat(),
                "search_result_url": hit.url,
                "search_result_title": hit.title,
                "fetched_url": page.url,
                "page_age": hit.page_age,
                "auto_published": post.is_published,
                "blocked_by": blocked,
                "adult_only_forced": force_adult_only(post),
            },
        }

        if not dry_run:
            try:
                # A savepoint per post: a race lost on the unique index, or any
                # other failure on the sixth article, must not discard the
                # first five.
                async with session.begin_nested():
                    session.add(post)
                    session.add(_interaction(response, prompt=hit.url, sources=[hit.url]))
                    await session.flush()
            except IntegrityError:
                logger.info("news ingest: lost a race on %s; counted as a duplicate", hit.url)
                result.skipped_duplicate += 1
                seen.add(digest)
                continue

        result.drafted += 1
        if blocked:
            result.skipped_gate += 1
        else:
            result.published += 1
        seen.add(digest)
        recent.append(post.title_i18n)

    return result
