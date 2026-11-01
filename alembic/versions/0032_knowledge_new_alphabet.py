"""Uzbek knowledge documents in the reformed alphabet.

Revision 0030 loaded the portal documents in the old Latin spelling. The
platform now writes Uzbek in the reformed alphabet (Ö ö, Ğ ğ, Ş ş, Ç ç), and
the knowledge base is what the assistant quotes and what the keyword fallback
searches, so the stored editions have to read the same way as the code. A
document holds only its metadata and the text lives in its chunks, so every
Uzbek document's title and every chunk of it are converted in place: the chunk
is what the answer is built from, the title is the source line under it.
Foreign words and protected names are left alone by the converter.

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.services.uzbek import to_new_alphabet, to_old_alphabet

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

documents = sa.table(
    "knowledge_documents",
    sa.column("id", sa.UUID(as_uuid=True)),
    sa.column("title", sa.String()),
    sa.column("language", sa.String()),
)
chunks = sa.table(
    "knowledge_chunks",
    sa.column("id", sa.UUID(as_uuid=True)),
    sa.column("document_id", sa.UUID(as_uuid=True)),
    sa.column("content", sa.Text()),
)


def _convert(convert) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(documents.c.id, documents.c.title).where(documents.c.language == "uz")
    ).all()
    for document_id, title in rows:
        new_title = convert(title)
        if new_title != title:
            bind.execute(
                documents.update().where(documents.c.id == document_id).values(title=new_title)
            )
        for chunk_id, content in bind.execute(
            sa.select(chunks.c.id, chunks.c.content).where(chunks.c.document_id == document_id)
        ).all():
            new_content = convert(content)
            if new_content != content:
                bind.execute(
                    chunks.update().where(chunks.c.id == chunk_id).values(content=new_content)
                )


def upgrade() -> None:
    _convert(to_new_alphabet)


def downgrade() -> None:
    _convert(to_old_alphabet)
