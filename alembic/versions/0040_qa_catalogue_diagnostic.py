"""QA audit fixes for the catalogue and the diagnostic.

* **Diagnostic version 4** (D-04, D-07, D-08, D-10, D-15): the version-3 bank
  with two self-ratings per Development Score dimension (the second taken from
  version 2), an optional "other goals" question, "at home" and "on maternity
  leave" as situations, and corrected wording. Inserted as new rows; version-3
  rows and every stored attempt stay as they are, so old results keep the
  scores they were given.
* **Stable catalogue order** (B-07): published programmes without a
  `published_at` get their `created_at`, so ordering by it is not a coin toss.
* **Option labels** (found in the QA audit): migration 0039 wrote v3 option
  words under `label`, the API reads `label_i18n`, so a database built by
  migrations served v3 options blank. Every bank's options are renamed to
  `label_i18n`; idempotent, and the readers accept both keys anyway.
* **Honest format** (G-16, B-06): `programs.format` may be empty — a partner
  platform that does not say is not "video" — and the portal's own courses
  marked video or audio with no media anywhere become text.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

# Migrations run from the repository checkout (/app/alembic in the image), where
# data/ sits beside alembic/ — the same layout 0039 relies on.
DATA = Path(__file__).resolve().parents[2] / "data" / "diagnostic_v4.json"
META_KEYS = ("show_if", "optional", "max_choices", "max_length", "section", "skill")


def _question_rows() -> list[dict]:
    bank = json.loads(DATA.read_text(encoding="utf-8"))
    rows = []
    for index, question in enumerate(bank["questions"], start=1):
        scored = question["type"] == "single_choice" and bool(question.get("dimension"))
        meta = {key: question[key] for key in META_KEYS if question.get(key) is not None}
        if question.get("hint"):
            meta["hint_i18n"] = question["hint"]
        options = []
        for option in question.get("options", []):
            # The key the API reads (`app.api.diagnostic`).
            row = {"id": option["id"], "label_i18n": option["label"]}
            if scored:
                row["score"] = option["score"]
                row["value"] = option["score"] * 25
            else:
                row["value"] = 0
            if "level" in option:
                row["level"] = option["level"]
            options.append(row)
        rows.append(
            {
                "id": uuid.uuid4(),
                "version": bank["version"],
                "code": question["code"],
                "dimension": question.get("dimension"),
                "order_index": index,
                "question_type": question["type"],
                "text_i18n": question["text"],
                "options": options,
                "meta": meta,
                "weight": 1.0 if scored else 0.0,
                "is_active": True,
            }
        )
    return rows


#: `label` → `label_i18n` inside each option; an option that already has
#: `label_i18n` keeps it. Running it twice changes nothing.
RENAME_LABELS = """
UPDATE assessment_questions q
SET options = (
    SELECT jsonb_agg(
        CASE
            WHEN jsonb_typeof(o) = 'object' AND o ? 'label'
            THEN (o - 'label')
                 || jsonb_build_object('label_i18n', COALESCE(o -> 'label_i18n', o -> 'label'))
            ELSE o
        END
        ORDER BY ord
    )
    FROM jsonb_array_elements(q.options) WITH ORDINALITY AS t(o, ord)
)
WHERE jsonb_typeof(q.options) = 'array'
  AND EXISTS (
    SELECT 1 FROM jsonb_array_elements(q.options) e
    WHERE jsonb_typeof(e) = 'object' AND e ? 'label'
  )
"""


def upgrade() -> None:
    op.execute(RENAME_LABELS)

    questions = sa.table(
        "assessment_questions",
        sa.column("id", sa.UUID()),
        sa.column("version", sa.Integer()),
        sa.column("code", sa.String()),
        sa.column("dimension", sa.String()),
        sa.column("order_index", sa.Integer()),
        sa.column("question_type", sa.String()),
        sa.column("text_i18n", postgresql.JSONB()),
        sa.column("options", postgresql.JSONB()),
        sa.column("meta", postgresql.JSONB()),
        sa.column("weight", sa.Float()),
        sa.column("is_active", sa.Boolean()),
    )
    op.bulk_insert(questions, _question_rows())

    op.execute(
        "UPDATE programs SET published_at = created_at WHERE is_published AND published_at IS NULL"
    )

    op.alter_column("programs", "format", existing_type=sa.String(length=20), nullable=True)
    # Imported courses: the platforms never said "video". The worker's next
    # import writes the same, this only saves the wait.
    op.execute("UPDATE programs SET format = NULL WHERE source IS NOT NULL AND format = 'video'")
    # Our own courses that claim video or audio and carry no media at all.
    op.execute(
        """
        UPDATE programs p SET format = 'text'
        WHERE p.source IS NULL AND p.format IN ('video', 'audio')
          AND NOT EXISTS (
            SELECT 1 FROM program_modules m
            LEFT JOIN program_lessons l ON l.module_id = m.id
            WHERE m.program_id = p.id
              AND (m.media_url IS NOT NULL OR l.media_url IS NOT NULL)
          )
        """
    )


def downgrade() -> None:
    # The `label` → `label_i18n` rename is kept: every reader accepts both.
    op.execute("UPDATE programs SET format = 'video' WHERE format IS NULL")
    op.alter_column("programs", "format", existing_type=sa.String(length=20), nullable=False)
    # published_at and the text formats are kept: both are true either way.
    op.execute(
        "DELETE FROM assessment_answers WHERE question_id IN "
        "(SELECT id FROM assessment_questions WHERE version = 4)"
    )
    op.execute("DELETE FROM assessment_questions WHERE version = 4")
