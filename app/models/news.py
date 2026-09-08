"""The news and announcements feed — the portal's front page once she is in.

This is the first screen after registration, so it carries three obligations
the rest of the catalogue does not.

* **Attribution.** A claim about medicine with no source behind it is a rumour,
  and a state portal repeating one is worse than a rumour. `source_name` is
  required on every published post.
* **The age boundary.** The portal is open to girls from ten. `is_adult_only`
  marks a post that belongs to adult care; the feed withholds it from a minor
  and from a reader whose age we do not know — the same protective default
  `age_gate` already applies to the assistant.
* **No verdicts.** These posts inform and point to a doctor or an official
  source; they never diagnose. Where a human drafts the post that stays an
  editorial rule, enforced by review before `is_published` is set. Where the
  AI news ingest drafts it, review is not available three times a day, so the
  rule is enforced in code by `services.news_ingest.publication_gate` — a
  check over attribution, the source domain, the escalation keyword filter and
  the vocabulary of a verdict, run before `is_published` is ever set. Neither
  the schema nor a prompt enforces it: a prompt is a request, and this feed
  needs a boundary.

Beside the gate sits a softer, separate mechanism: the editorial scores at the
bottom of the model. `age_relevance` says which age brackets an article is
*for*, `topics` says which women's-health subjects it covers, and the three
score columns carry the AI editor's reading of it. None of them withholds
anything — they only order the personalised "For you" section, while the feed,
the categories and the search stay complete for everyone. See
`services.news_age` and `services.news_ranking`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import NewsCategory
from app.models.base import Base, TimestampMixin, UUIDMixin, str_enum


class NewsPost(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "news_posts"
    __table_args__ = (
        Index("ix_news_posts_published", "is_published", "published_at"),
        Index("ix_news_posts_category_published", "category", "is_published"),
        # Deduplication expressed as a constraint rather than as a hope: even
        # if two ingest runs raced past the SELECT, the second INSERT fails at
        # the database instead of producing a duplicate feed card.
        Index("ix_news_posts_source_url_hash", "source_url_hash", unique=True),
    )

    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    category: Mapped[NewsCategory] = mapped_column(
        str_enum(NewsCategory, 20), nullable=False, index=True
    )

    # Translatable like every other content field on the portal; the reader
    # falls back uz -> ru -> en.
    title_i18n: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    summary_i18n: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False, comment="The card text in the feed."
    )
    body_i18n: Mapped[dict] = mapped_column(
        JSONB, default=dict, nullable=False, comment="Paragraphs of the full post."
    )

    # A photograph, when the editor has one. It must be served from our own
    # storage (the S3/MinIO bucket the stack already provisions) rather than
    # hotlinked from a news site: an image fetched from a third party is a
    # request from every reader's browser to that third party, on every scroll,
    # with no consent behind it — and it disappears the day they move the file.
    cover_url: Mapped[str | None] = mapped_column(String(500))

    # The fallback, and what most posts use. Drawn rather than fetched, so a
    # post is never blank and the feed still works on a slow regional
    # connection. `cover_tone` picks a gradient from the site palette and
    # `cover_emblem` is the glyph drawn on it.
    cover_tone: Mapped[str] = mapped_column(String(20), default="plum", nullable=False)
    cover_emblem: Mapped[str] = mapped_column(String(8), default="✦", nullable=False)

    source_name: Mapped[str | None] = mapped_column(String(160))
    source_url: Mapped[str | None] = mapped_column(String(500))

    # The deduplication key, and the only one that holds forever. A job that
    # runs three times a day for years cannot rely on a title: the same story
    # is retitled between outlets and reprinted on anniversaries. This is the
    # canonical form of `source_url` — scheme and host lowercased, `www.`
    # dropped, query and fragment removed, trailing slash trimmed — hashed so
    # the unique index is fixed-width. Null for hand-written posts, and
    # Postgres allows many nulls in a unique index.
    source_url_hash: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    region: Mapped[str | None] = mapped_column(String(60), index=True)
    reading_minutes: Mapped[int | None] = mapped_column(Integer)

    # See the module docstring: a minor never sees these.
    is_adult_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    author_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # --- editorial scoring, for ranking only ---------------------------
    #
    # Everything below moves a post up or down the personalised section. None
    # of it hides a post: `list_news` never reads these columns.

    # {"13-17": 0, ..., "55+": 90} — how much this post is *for* each bracket.
    # Empty means nobody has scored it yet, and `news_age.derive_age_relevance`
    # supplies a rule-based map on the fly. Storing it rather than deriving it
    # every time is what lets an editor or the AI overrule the keyword rules
    # for a post where they read it wrong.
    age_relevance: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # `MedicalTopic` values. The axis age relevance turns on: "health" cannot
    # separate adolescent periods from osteoporosis, and those two sit at
    # opposite ends of the age range.
    topics: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)

    # The three editorial judgements from section 06 of the brief, 0-100.
    # Nullable because "not yet assessed" and "assessed as zero" are different
    # facts, and the ranker substitutes a default for the first.
    relevance_score: Mapped[int | None] = mapped_column(Integer)
    credibility_score: Mapped[int | None] = mapped_column(Integer)
    impact_score: Mapped[int | None] = mapped_column(Integer)

    # Which model said so, under which prompt version, and when — the trace
    # every AI output on this portal has to carry.
    ai_meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
