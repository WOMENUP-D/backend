"""The portal documents, re-read after the partners changed.

The assistant quotes `app.knowledge_base`. Its editions on jobs, business
support and data sharing named Edu-Job and Invest HUB; the portal now reads
vacancies from hh.uz, where a woman applies on hh.uz itself, and its business
partners are Wbip.uz and Tijoratmarkazi.uz. The texts were corrected in code
and this revision loads them into every installation, the way 0030 first
loaded them: a document that exists for a title and language has its chunks
replaced, one that does not is created, and nothing a trainer uploaded is
touched.

There is no way back to the earlier wording from the database, and none is
wanted: `downgrade` leaves the documents as they are.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.knowledge_base import KNOWLEDGE, KNOWLEDGE_TRANSLATIONS
from app.services.rag import chunk_text

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

documents = sa.table(
    "knowledge_documents",
    sa.column("id", sa.UUID(as_uuid=True)),
    sa.column("title", sa.String()),
    sa.column("source_type", sa.String()),
    sa.column("language", sa.String()),
    sa.column("tags", postgresql.ARRAY(sa.String())),
    sa.column("is_approved", sa.Boolean()),
    sa.column("approved_at", sa.DateTime(timezone=True)),
    sa.column("version", sa.Integer()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
chunks = sa.table(
    "knowledge_chunks",
    sa.column("id", sa.UUID(as_uuid=True)),
    sa.column("document_id", sa.UUID(as_uuid=True)),
    sa.column("chunk_index", sa.Integer()),
    sa.column("content", sa.Text()),
    sa.column("token_count", sa.Integer()),
    sa.column("meta", postgresql.JSONB()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


def _editions():
    for title, source_type, body in KNOWLEDGE:
        yield "uz", title, source_type, body
        for language, (edition_title, edition_body) in KNOWLEDGE_TRANSLATIONS.get(
            title, {}
        ).items():
            yield language, edition_title, source_type, edition_body


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC)
    for language, title, source_type, body in _editions():
        existing = bind.execute(
            sa.select(documents.c.id, documents.c.tags).where(
                documents.c.title == title, documents.c.language == language
            )
        ).first()
        if existing is None:
            document_id = uuid.uuid4()
            bind.execute(
                documents.insert().values(
                    id=document_id,
                    title=title,
                    source_type=source_type,
                    language=language,
                    tags=["portal"],
                    is_approved=True,
                    approved_at=now,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            document_id = existing[0]
            tags = sorted(set(existing[1] or []) | {"portal"})
            bind.execute(
                documents.update()
                .where(documents.c.id == document_id)
                .values(tags=tags, is_approved=True, approved_at=now, updated_at=now)
            )
            bind.execute(chunks.delete().where(chunks.c.document_id == document_id))
        for index, piece in enumerate(chunk_text(body.strip())):
            bind.execute(
                chunks.insert().values(
                    id=uuid.uuid4(),
                    document_id=document_id,
                    chunk_index=index,
                    content=piece,
                    token_count=len(piece.split()),
                    meta={},
                    created_at=now,
                    updated_at=now,
                )
            )


def downgrade() -> None:
    """The documents keep their current text; see the module docstring."""
