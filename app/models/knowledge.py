"""RAG knowledge base: approved documents chunked and embedded.

The AI navigator answers only from this table (section 06: "tasdiqlangan
manbalardan javob berish"), so an unapproved chunk can never be retrieved.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.models.base import Base, TimestampMixin, UUIDMixin


class KnowledgeDocument(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_documents"

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="program | guideline | faq | legal | classifier | partner_catalog",
    )
    source_url: Mapped[str | None] = mapped_column(String(500))
    language: Mapped[str] = mapped_column(String(5), default="uz", nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)

    # Nothing is retrievable until a moderator approves it.
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class KnowledgeChunk(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_knowledge_chunks_document_order", "document_id", "chunk_index"),)

    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dimensions))
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")
