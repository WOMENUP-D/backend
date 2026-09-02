"""The AI news editor: what a post is about, and who it is for.

One job — read a post and return the editorial judgements the ranker needs:
the women's-health subtopics it covers, an age-relevance score for each of the
six brackets, and its relevance, credibility and impact.

Two rules from the portal's AI guardrails shape the whole module.

* **Degrade, don't fail.** With no API key, a rate limit, a refusal or an
  unparsable reply, this returns the keyword-based reading from
  `news_age.derive_age_relevance` instead of an error. The feed is the first
  screen after registration; it does not get to be unavailable because a model
  is. `Analysis.source` records which path produced the numbers.
* **Every AI output carries its trace.** Model, prompt version and trace id go
  into `NewsPost.ai_meta` beside the scores, so a ranking decision months from
  now can be traced to the reading that caused it.

The model is asked, the code enforces: every number that comes back is clamped
to 0-100 and every topic checked against the enum before it is stored. A
structured-output schema constrains shape, not values.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.constants import AgeGroup, MedicalTopic, NewsCategory
from app.models.news import NewsPost
from app.services.llm_gateway import LlmUnavailableError, llm_gateway
from app.services.news_age import (
    AGE_GROUPS,
    derive_age_relevance,
    detect_medical_topics,
    searchable_text,
)
from app.services.prompts import NEWS_ANALYSIS_SCHEMA, NEWS_EDITOR_SYSTEM

logger = logging.getLogger(__name__)

# How much of a post the editor is shown. The judgement rests on subject and
# framing, both of which are settled long before the end of a long article,
# and sending the whole body of every post is the difference between a
# reasonable analysis pass and an expensive one.
BODY_EXCERPT = 1200


@dataclass(frozen=True)
class Analysis:
    """One post's editorial reading.

    `source` is part of the result, not a debugging detail: "the model scored
    this" and "keyword rules scored this" are different claims about the same
    numbers, and the second one is what an editor should see before trusting a
    surprising ranking.
    """

    topics: list[MedicalTopic]
    age_relevance: dict[str, int]
    relevance: int
    credibility: int | None
    impact: int
    audience: str = ""
    rationale: str = ""
    source: str = "rules"  # "model" | "rules"
    meta: dict[str, Any] = field(default_factory=dict)


def _clamp(value: Any, fallback: int) -> int:
    """A score from the model, made safe. Anything unusable becomes fallback."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    return max(0, min(100, int(value)))


def rule_based(post: NewsPost) -> Analysis:
    """The reading the portal falls back to, and the one it starts from.

    Not a stub. This is a complete analysis derived from the post's own text —
    the same derivation the feed uses for any post the model has never seen —
    so an installation with no API key at all still gets age-aware ordering.
    """
    text = searchable_text(post.title_i18n, post.summary_i18n, list(post.tags or []))
    topics = detect_medical_topics(text)
    return Analysis(
        topics=topics,
        age_relevance=derive_age_relevance(
            category=post.category,
            title_i18n=post.title_i18n,
            summary_i18n=post.summary_i18n,
            tags=list(post.tags or []),
            topics=topics,
        ),
        relevance=70,
        # Left to the ranker, which reads it off the named source. Inventing a
        # credibility figure without a model having judged anything would be
        # dressing a default up as an assessment.
        credibility=None,
        impact=90 if post.is_pinned else 60,
        source="rules",
    )


def _excerpt(body_i18n: dict | None) -> str:
    if not body_i18n:
        return ""
    for lang in ("uz", "ru", "en"):
        text = body_i18n.get(lang)
        if text:
            return str(text)[:BODY_EXCERPT]
    first = next((value for value in body_i18n.values() if value), "")
    return str(first)[:BODY_EXCERPT]


def _user_message(post: NewsPost) -> str:
    """The article as the editor sees it.

    The source is named rather than summarised away: credibility is one of the
    judgements being asked for, and it cannot be made without knowing who is
    speaking.
    """
    category = post.category.value if isinstance(post.category, NewsCategory) else post.category
    lines = [
        f"Category: {category}",
        f"Source: {post.source_name or 'not stated'}",
        f"Tags: {', '.join(post.tags or []) or 'none'}",
        f"Marked adult-only by an editor: {'yes' if post.is_adult_only else 'no'}",
        "",
        "Title (all locales):",
        *(f"  {lang}: {value}" for lang, value in (post.title_i18n or {}).items() if value),
        "",
        "Summary (all locales):",
        *(f"  {lang}: {value}" for lang, value in (post.summary_i18n or {}).items() if value),
        "",
        "Body excerpt:",
        _excerpt(post.body_i18n),
        "",
        "Available women's-health subtopics: " + ", ".join(topic.value for topic in MedicalTopic),
        "Age brackets to score: " + ", ".join(group.value for group in AGE_GROUPS),
    ]
    return "\n".join(lines)


def _parse(
    payload: dict[str, Any], fallback: Analysis
) -> tuple[list[MedicalTopic], dict[str, int]]:
    """Topics and the age map out of a model reply, both checked.

    An unusable age map falls back to the rule-based one wholesale rather than
    being patched bracket by bracket: a half-parsed map is a ranking nobody can
    account for, and the rules already produce a defensible one.
    """
    topics: list[MedicalTopic] = []
    for value in payload.get("topics") or []:
        try:
            topics.append(MedicalTopic(value))
        except ValueError:
            logger.warning("news editor returned an unknown topic", extra={"topic": value})

    raw = payload.get("age_relevance")
    if not isinstance(raw, dict):
        return topics, fallback.age_relevance

    scores: dict[str, int] = {}
    for group in AgeGroup:
        value = raw.get(group.value)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return topics, fallback.age_relevance
        scores[group.value] = max(0, min(100, int(value)))
    return topics, scores


async def analyse(post: NewsPost) -> Analysis:
    """Read a post. Never raises; falls back to the keyword rules."""
    fallback = rule_based(post)

    if not llm_gateway.enabled:
        return fallback

    try:
        response = await llm_gateway.complete(
            system=NEWS_EDITOR_SYSTEM,
            messages=[{"role": "user", "content": _user_message(post)}],
            json_schema=NEWS_ANALYSIS_SCHEMA,
        )
    except LlmUnavailableError as exc:
        logger.warning("news editor unavailable, using keyword rules: %s", exc)
        return fallback

    if response.refused or not response.parsed:
        logger.warning(
            "news editor produced no usable analysis",
            extra={"extra_fields": {"trace_id": response.trace_id, "slug": post.slug}},
        )
        return fallback

    payload = response.parsed
    topics, age_relevance = _parse(payload, fallback)

    return Analysis(
        topics=topics,
        age_relevance=age_relevance,
        relevance=_clamp(payload.get("relevance"), fallback.relevance),
        credibility=_clamp(payload.get("credibility"), 70),
        impact=_clamp(payload.get("impact"), fallback.impact),
        audience=str(payload.get("audience") or "")[:500],
        rationale=str(payload.get("rationale") or "")[:1000],
        source="model",
        meta={
            "model": response.model,
            "prompt_version": response.prompt_version,
            "trace_id": response.trace_id,
            "analysed_at": datetime.now(UTC).isoformat(),
        },
    )


def apply_analysis(post: NewsPost, analysis: Analysis) -> None:
    """Write a reading onto the post.

    `credibility_score` is only set when something actually judged it. Left
    null, the ranker derives it from the named source, which is the more honest
    answer than storing a number nobody stands behind.
    """
    post.topics = [topic.value for topic in analysis.topics]
    post.age_relevance = dict(analysis.age_relevance)
    post.relevance_score = analysis.relevance
    post.impact_score = analysis.impact
    if analysis.credibility is not None:
        post.credibility_score = analysis.credibility
    post.ai_meta = {
        **analysis.meta,
        "source": analysis.source,
        "audience": analysis.audience,
        "rationale": analysis.rationale,
    }
