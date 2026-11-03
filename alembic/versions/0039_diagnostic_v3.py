"""Diagnostic version 3: who she is and what she wants, before how she scores.

Version 2 asked twenty-five self-ratings and a goals list; nothing it asked
said what a woman does, what she is after, which fields draw her, what she
already knows or what stands in her way — and so nothing downstream could
use it. Version 3 opens with that profile block (adaptive: a student and an
entrepreneur see different questions), keeps two of the three self-ratings
per Development Score dimension so the score stays comparable, and stores
the derived profile and the course recommendation built from it on the
attempt. Version-2 questions and attempts stay as they are; results remain
readable.

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

DATA = Path(__file__).resolve().parents[2] / "data" / "diagnostic_v3.json"
META_KEYS = ("show_if", "optional", "max_choices", "max_length", "section", "skill")


def _question_rows() -> list[dict]:
    bank = json.loads(DATA.read_text(encoding="utf-8"))
    rows = []
    for index, question in enumerate(bank["questions"], start=1):
        scored = question["type"] == "single_choice" and question.get("dimension")
        meta = {key: question[key] for key in META_KEYS if question.get(key) is not None}
        if question.get("hint"):
            meta["hint_i18n"] = question["hint"]
        options = []
        for option in question.get("options", []):
            row = {"id": option["id"], "label": option["label"]}
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


def upgrade() -> None:
    op.add_column("assessments", sa.Column("profile", postgresql.JSONB(), nullable=True))
    op.add_column("assessments", sa.Column("recommendation", postgresql.JSONB(), nullable=True))
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


def downgrade() -> None:
    op.execute(
        "DELETE FROM assessment_answers WHERE question_id IN "
        "(SELECT id FROM assessment_questions WHERE version = 3)"
    )
    op.execute("DELETE FROM assessment_questions WHERE version = 3")
    op.drop_column("assessments", "recommendation")
    op.drop_column("assessments", "profile")
