"""Retrieval over the approved knowledge base.

Only chunks whose parent document is approved are ever returned, which is what
keeps the navigator's answers inside the vetted corpus.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.services import safety
from app.services.uzbek import to_new_alphabet, to_old_alphabet


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    content: str
    score: float

    @property
    def excerpt(self) -> str:
        return self.content[:280] + ("…" if len(self.content) > 280 else "")


def needs_human_escalation(text: str) -> bool:
    """Model-free pre-filter for disclosures of violence or self-harm.

    Deliberately runs before the model: a user disclosing violence should reach
    a human without her message being sent to a generative system first. The
    lexicons, normalisation and the regression corpus live in `safety`.
    """
    return safety.detect(text) is not None


def mentions_safety_topic(text: str) -> bool:
    """Broader screen for editorial material (news): any mention of the
    subjects, not only a first-person disclosure."""
    return safety.mentions_safety_topic(text)


async def retrieve(
    session: AsyncSession,
    query: str,
    *,
    language: str | None = None,
    top_k: int | None = None,
    query_embedding: list[float] | None = None,
) -> list[RetrievedChunk]:
    """Fetch the most relevant approved chunks.

    Uses vector similarity when an embedding is supplied, and PostgreSQL
    full-text search otherwise — so the navigator degrades to keyword search
    rather than failing when the embedding provider is unavailable.
    """
    limit = top_k or settings.ai_rag_top_k

    if query_embedding is not None:
        distance = KnowledgeChunk.embedding.cosine_distance(query_embedding)
        stmt = (
            select(
                KnowledgeChunk.id,
                KnowledgeChunk.document_id,
                KnowledgeDocument.title,
                KnowledgeChunk.content,
                (1 - distance).label("score"),
            )
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(
                KnowledgeDocument.is_approved.is_(True),
                KnowledgeChunk.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(limit)
        )
    else:
        ts_vector = func.to_tsvector("simple", KnowledgeChunk.content)
        ts_query = func.to_tsquery("simple", _or_query(query))
        stmt = (
            select(
                KnowledgeChunk.id,
                KnowledgeChunk.document_id,
                KnowledgeDocument.title,
                KnowledgeChunk.content,
                func.ts_rank(ts_vector, ts_query).label("score"),
            )
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(
                KnowledgeDocument.is_approved.is_(True),
                ts_vector.op("@@")(ts_query),
            )
            .order_by(func.ts_rank(ts_vector, ts_query).desc())
            .limit(limit)
        )

    if language:
        stmt = stmt.where(KnowledgeDocument.language == language)

    rows = await session.execute(stmt)
    return [
        RetrievedChunk(
            chunk_id=row[0],
            document_id=row[1],
            document_title=row[2],
            content=row[3],
            score=float(row[4] or 0.0),
        )
        for row in rows
    ]


def _or_query(query: str) -> str:
    """Turn free text into an OR-joined tsquery.

    `plainto_tsquery` ANDs every term, so one word absent from a chunk drops the
    whole match. For a keyword fallback recall matters far more than precision —
    ranking sorts the rest out.
    """
    words = [
        re.sub(r"[^\w\u0400-\u04ff\u02bb\u2018\u2019-]", "", word) for word in query.lower().split()
    ]
    terms: list[str] = []
    for word in words:
        if len(word) <= 2:
            continue
        # Uzbek is stored in the reformed alphabet but typed in either; a term
        # is searched in both spellings so neither keyboard loses recall.
        for spelling in (word, to_new_alphabet(word), to_old_alphabet(word)):
            if spelling and spelling not in terms:
                terms.append(spelling)
    return " | ".join(terms) if terms else "''"


def build_context(chunks: list[RetrievedChunk]) -> str:
    """Render retrieved chunks as a citable context block."""
    if not chunks:
        return "CONTEXT: (empty — no approved source covers this question)"
    parts = [
        f"[source:{chunk.chunk_id}] {chunk.document_title}\n{chunk.content}" for chunk in chunks
    ]
    return "CONTEXT:\n\n" + "\n\n---\n\n".join(parts)


def chunk_text(text: str, *, size: int = 1200, overlap: int = 200) -> list[str]:
    """Split a document on paragraph boundaries, with overlap for continuity."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 <= size:
            current = f"{current}\n\n{paragraph}" if current else paragraph
            continue
        if current:
            chunks.append(current)
        # A single oversized paragraph is hard-split with overlap.
        if len(paragraph) > size:
            step = size - overlap
            for start in range(0, len(paragraph), step):
                piece = paragraph[start : start + size]
                if piece.strip():
                    chunks.append(piece.strip())
            current = ""
        else:
            current = paragraph

    if current:
        chunks.append(current)
    return chunks
