"""Courses chosen for marketplace vacancies.

The vacancy marketplace (`/api/v1/vacancies`) reads listings live from the
configured job board and keeps none of them. The one thing the portal adds
to a listing — the courses it chose for the listing's requirements — needs a
home of its own, keyed by the board and the board's own id, so the choice is
made once per listing and not once per visitor.

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vacancy_course_suggestions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("external_id", sa.String(length=120), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "provider", "external_id", name="uq_vacancy_course_suggestions_listing"
        ),
    )


def downgrade() -> None:
    op.drop_table("vacancy_course_suggestions")
