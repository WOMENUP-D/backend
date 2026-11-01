"""Vacancies from hh.uz, and the courses chosen for each one.

The portal's opportunities table has been empty on the live site: no partner
feed delivers listings yet. The worker now reads open vacancies for
Uzbekistan from the public hh.uz API into the same table under the source
`hh_uz` (`services.hh_jobs`). The source column is a checked varchar, so the
new value needs no schema change.

What does: for every vacancy the portal chooses the courses in its catalogue
that teach what the vacancy asks for (`services.vacancy_courses`). The choice
is about the listing and not about a reader, so it is cached on the listing
itself — with the mode it was made in, the model and prompt versions and the
trace id, as every AI suggestion on the platform must be.

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "opportunities",
        sa.Column("course_suggestions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunities", "course_suggestions")
