"""Courses that live somewhere else.

The catalogue could only hold programmes the portal runs itself, so while its
own are being written it had nothing to show. These two columns let it also
list somebody else's — a Stepik course, a university's — with the address it
lives at and the catalogue it came from.

Both are nullable and nothing reads them unless they are set, so every
programme already in the table is untouched.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("programs", sa.Column("external_url", sa.String(length=500), nullable=True))
    op.add_column("programs", sa.Column("source", sa.String(length=40), nullable=True))
    # Imports are undone by source, so it is worth an index of its own.
    op.create_index("ix_programs_source", "programs", ["source"])


def downgrade() -> None:
    op.drop_index("ix_programs_source", table_name="programs")
    op.drop_column("programs", "source")
    op.drop_column("programs", "external_url")
