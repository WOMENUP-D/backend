"""The portal's documentation, loaded for the AI assistant.

The fourteen approved documents the assistant quotes about WomanUP itself —
registration, the Development Score, courses, applications, data handling, the
assistant's own limits — only ever reached a database through the
demonstration seed, which refuses to run in production. So on the live site a
woman asking "what is WomanUP?" was told the assistant did not know.

Loads every edition (uz, ru, en) from `app.knowledge_base`, approved and tagged
`portal`. Idempotent: a document that already exists for a title and language
has its text replaced, so a corrected sentence reaches every installation on
the next release; nothing a trainer uploaded herself is touched.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.knowledge_base import KNOWLEDGE, KNOWLEDGE_TRANSLATIONS
from app.services.rag import chunk_text

revision = "0030"
down_revision = "0029"
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
    """Remove only what this revision added: documents tagged `portal` and
    nothing else. A seeded or hand-uploaded document keeps its chunks."""
    bind = op.get_bind()
    titles = [title for _, title, _, _ in _editions()]
    rows = bind.execute(
        sa.select(documents.c.id, documents.c.tags).where(documents.c.title.in_(titles))
    ).all()
    for document_id, tags in rows:
        if tags and "portal" in tags and len(tags) == 1:
            bind.execute(chunks.delete().where(chunks.c.document_id == document_id))
            bind.execute(documents.delete().where(documents.c.id == document_id))
        elif tags and "portal" in tags:
            bind.execute(
                documents.update()
                .where(documents.c.id == document_id)
                .values(tags=[tag for tag in tags if tag != "portal"])
            )
