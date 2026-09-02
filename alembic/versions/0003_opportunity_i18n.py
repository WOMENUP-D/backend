"""Opportunity titles and descriptions become translatable.

They were plain strings while every other user-facing content field on the
portal was already a JSONB i18n map, which made the opportunities catalogue the
one screen that stayed Uzbek whatever locale the user picked.

Partner platforms still send a single-language string; the sync stores it under
the language it arrived in, and the reader falls back the same way the rest of
the portal does.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "opportunities",
        sa.Column(
            "title_i18n",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "opportunities",
        sa.Column(
            "description_i18n",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )

    # Existing rows are Uzbek — that is the primary locale and the only one the
    # catalogue was ever authored in.
    op.execute(
        """
        UPDATE opportunities
           SET title_i18n = jsonb_build_object('uz', title),
               description_i18n = CASE
                   WHEN description IS NULL OR description = '' THEN '{}'::jsonb
                   ELSE jsonb_build_object('uz', description)
               END
        """
    )

    op.drop_column("opportunities", "title")
    op.drop_column("opportunities", "description")


def downgrade() -> None:
    op.add_column("opportunities", sa.Column("title", sa.String(length=300), nullable=True))
    op.add_column("opportunities", sa.Column("description", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE opportunities
           SET title = coalesce(title_i18n->>'uz', title_i18n->>'ru', title_i18n->>'en', ''),
               description = coalesce(
                   description_i18n->>'uz', description_i18n->>'ru', description_i18n->>'en'
               )
        """
    )
    op.alter_column("opportunities", "title", nullable=False)
    op.drop_column("opportunities", "title_i18n")
    op.drop_column("opportunities", "description_i18n")
