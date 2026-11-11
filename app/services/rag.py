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
from app.services.uzbek import to_new_alphabet, to_old_alphabet


# Safety topics that bypass the model entirely and go straight to a human.
# Apostrophes in Uzbek Latin are typed as any of ʻ ʼ ‘ ’ ' — and often omitted
# entirely. Matching is done on a form with all of them stripped, so a single
# spelling below covers every variant a user might type.
def _fold(text: str) -> str:
    """Lowercase, fold the reformed letters back to their base form and remove
    apostrophes, so o'zimni / oʻzimni / ozimni / özimni all agree.

    The keyword list below is written in the old spelling without apostrophes;
    a disclosure typed in the reformed alphabet (ö ğ ş ç) must reach the same
    words, or the safety gate would depend on which keyboard she has.
    """
    lowered = text.lower()
    for new, old in (("ö", "o"), ("ğ", "g"), ("ş", "sh"), ("ç", "ch")):
        lowered = lowered.replace(new, old)
    for mark in ("\u02bb", "\u02bc", "\u2018", "\u2019", "\u0027", "`"):
        lowered = lowered.replace(mark, "")
    return lowered


# High-risk topics that must reach a human without passing through the model.
# Written in folded form (no apostrophes) to match `_fold` output. Uzbek verbs
# appear in several tenses on purpose: a disclosure is far more often made in
# the present tense ("uradi") than the past, and the past-only list this
# started as let the most common phrasing straight through to the model.
ESCALATION_KEYWORDS: tuple[str, ...] = (
    # --- uz: physical violence ---
    "zoravonlik",
    "kaltakla",
    "meni uradi",
    "meni urdi",
    "meni uryapti",
    "urib turadi",
    "doppasla",
    "bolamni urdi",
    "bolamni uradi",
    "oiladagi zorav",
    # --- uz: coercion and threat ---
    "majburlayapti",
    "majburlamoqda",
    "tahdid qilyapti",
    "tahdid qilmoqda",
    "qorqityapti",
    # --- uz: sexual violence ---
    "jinsiy tazyiq",
    "jinsiy zorav",
    "tajovuz",
    # --- uz: self-harm ---
    "ozimni oldir",
    "oz joniga qasd",
    "yashagim kelmayapti",
    "olib qoysam",
    # --- uz: fraud ---
    "firibgar",
    # --- ru ---
    "насилие",
    "насилует",
    "избил",
    "избивает",
    "бьет",
    "бьёт",
    "ударил",
    "угрожает",
    "изнасилов",
    "принуждает",
    "покончить с собой",
    "суицид",
    "не хочу жить",
    "мошенник",
    # --- en ---
    "violence",
    "abuse",
    "beats me",
    "hits me",
    "threatens me",
    "rape",
    "suicide",
    "kill myself",
    "self-harm",
    "want to die",
)


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
    """Keyword pre-filter for high-risk topics.

    Deliberately runs before the model: a user disclosing violence should reach
    a human without her message being sent to a generative system first.
    """
    folded = _fold(text)
    return any(keyword in folded for keyword in ESCALATION_KEYWORDS)


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
        # The document's title is what it is about; a match there counts more
        # than one more passing mention in a long body.
        title_vector = func.to_tsvector("simple", KnowledgeDocument.title)
        ts_query = func.to_tsquery("simple", _or_query(query))
        score = func.ts_rank(ts_vector, ts_query) + func.ts_rank(title_vector, ts_query)
        stmt = (
            select(
                KnowledgeChunk.id,
                KnowledgeChunk.document_id,
                KnowledgeDocument.title,
                KnowledgeChunk.content,
                score.label("score"),
            )
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .where(
                KnowledgeDocument.is_approved.is_(True),
                ts_vector.op("@@")(ts_query) | title_vector.op("@@")(ts_query),
            )
            .order_by(score.desc())
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


#: Words every question is made of and no document is about. Left in, "qanday"
#: outranked "sertifikat": the one matching word in a privacy page beat the
#: page about certificates. Folded, as `_fold` writes them.
_QUESTION_WORDS = frozenset(
    # uz
    "qanday qanaqa qaysi qayerda qayerdan qachon nima nimaga nega necha kim men menga "  # noqa: SIM905
    "mening meni bilan uchun kerak mumkin bormi yoki ham haqida qilib qilsam boladi "
    "bolsa iltimos portalda portal womanup "
    # ru
    "как что где когда какие какой какая каких можно нужно ли для это мне меня мой моя "
    "мои после есть или чтобы если про на портале портал womanup "
    # en
    "how what which where when why who the and for can could does did will would should "
    "with from about there this that your you my any get"
    "".split()
)

#: Uzbek is agglutinative: "sertifikatni", "sertifikatga" and "sertifikatlar"
#: are one word to her and three to an exact-token search. Longest first.
_UZ_SUFFIXES = tuple(
    sorted(
        (  # noqa: SIM905
            "laringiz larimiz larning larni larga larda lardan lari lar "
            "ingizni ingizga ingiz imizni imiz imni imga im sini mni ning si ni ga ka qa m "
            "dagi dan da ta mi man aman yapman yapti moqda miz siz "
            "ish iş gan"
        ).split(),
        key=len,
        reverse=True,
    )
)
_RU_ENDINGS = re.compile(
    r"(?:иями|ями|ами|ого|его|ому|ему|ыми|ими|ией|ов|ев|ей|ам|ям|ах|ях|ом|ем|ой|ий|ый|ая|ое|"
    r"ые|ую|ию|ия|ие|ии|ть|а|я|у|ю|ы|и|е|о)$"
)
_EN_ENDINGS = re.compile(r"(?:ing|ed|es|s)$")
_MIN_STEM = 4


def _stem(word: str) -> str:
    """A light stem, so a prefix query reaches the other forms of the word.

    Deliberately crude — a handful of endings per language, never below four
    letters — because it only widens a keyword fallback that ranking then
    orders; it is not a morphological analyser.
    """
    if re.search("[\u0400-\u04ff]", word):
        stemmed = _RU_ENDINGS.sub("", word)
        return stemmed if len(stemmed) >= _MIN_STEM else word
    stemmed = word
    for _ in range(3):
        for suffix in _UZ_SUFFIXES:
            if stemmed.endswith(suffix) and len(stemmed) - len(suffix) >= _MIN_STEM:
                stemmed = stemmed[: -len(suffix)]
                break
        else:
            break
    if stemmed == word:
        english = _EN_ENDINGS.sub("", word)
        if len(english) >= _MIN_STEM:
            stemmed = english
    return stemmed


def _or_query(query: str) -> str:
    """Turn free text into an OR-joined tsquery.

    `plainto_tsquery` ANDs every term, so one word absent from a chunk drops the
    whole match. For a keyword fallback recall matters far more than precision —
    ranking sorts the rest out. Each word is searched as written and, through
    a light stem, as a prefix (`sertifikat:*`), so "sertifikatni" finds the
    page that says "sertifikat". Question words are dropped unless nothing else
    is left.
    """
    words = [
        re.sub(r"[^\w\u0400-\u04ff\u02bb\u2018\u2019]", "", part)
        for word in query.lower().split()
        for part in word.split("-")
    ]
    words = [word for word in words if len(word) > 2]
    content = [word for word in words if _fold(word) not in _QUESTION_WORDS]
    terms: list[str] = []

    def add(term: str) -> None:
        if term and term not in terms:
            terms.append(term)

    for word in content or words:
        # Uzbek is stored in the reformed alphabet but typed in either; a term
        # is searched in both spellings so neither keyboard loses recall.
        for spelling in (word, to_new_alphabet(word), to_old_alphabet(word)):
            add(spelling)
            stem = _stem(spelling)
            if stem != spelling or len(spelling) >= _MIN_STEM + 2:
                add(f"{stem}:*")
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
