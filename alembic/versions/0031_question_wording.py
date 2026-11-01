"""One diagnostic question, reworded in Russian.

Question lea.q23 asked "Насколько у вас есть опыт…", which is not Russian.
`data/diagnostic_v2.json` carries the corrected sentence for fresh
installations; this revision brings an installation that loaded the question
under 0028 to the same words. Text only — codes, options, weights and every
recorded answer stay as they are.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

OLD = "Насколько у вас есть опыт лидерства или проектной работы?"
NEW = "Насколько большой у вас опыт лидерства или проектной работы?"


def _set_ru(text: str) -> None:
    op.execute(
        sa.text(
            "UPDATE assessment_questions "
            "SET text_i18n = jsonb_set(text_i18n, '{ru}', to_jsonb(CAST(:text AS text))) "
            "WHERE version = 2 AND code = 'lea.q23'"
        ).bindparams(text=text)
    )


def upgrade() -> None:
    _set_ru(NEW)


def downgrade() -> None:
    _set_ru(OLD)
