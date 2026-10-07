"""Diagnostic v2: eight new dimensions, the 25-question instrument, attempts
that keep what they measured, and score adjustments earned by doing things.

The dimension set changes. Digital skills is new; social activity and
international integration become one leadership & global opportunities
dimension. Every stored dimension is rewritten in the same step, because a row
still holding an old value would fail to load once the code no longer knows it:

* `development_scores` — her two old rows become one `leadership` row, each of
  baseline / current / target the mean of the two;
* `plan_items`, `goals`, `learning_paths`, version-1 `assessment_questions` —
  renamed in place;
* `skills.dimensions` — renamed and de-duplicated, and every digital-category
  skill now also counts towards digital skills.

Version-1 questions are retired, not deleted: historical answers still point
at them. Version-2 questions are loaded from `data/diagnostic_v2.json`.

The downgrade restores the schema and maps `leadership` back to
`social_activity`; digital-skills rows are removed. Merged scores cannot be
split again.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

DATA = Path(__file__).resolve().parents[2] / "data" / "diagnostic_v2.json"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def _question_rows() -> list[dict]:
    """The version-2 instrument as `assessment_questions` rows."""
    bank = json.loads(DATA.read_text(encoding="utf-8"))
    rows = []
    for index, question in enumerate(bank["questions"], start=1):
        scored = question["type"] == "single_choice"
        meta = {}
        if question.get("hint"):
            meta["hint_i18n"] = question["hint"]
        if question.get("max_choices"):
            meta["max_choices"] = question["max_choices"]
        rows.append(
            {
                "id": uuid.uuid4(),
                "version": bank["version"],
                "code": question["code"],
                "dimension": question["dimension"],
                "order_index": index,
                "question_type": question["type"],
                "text_i18n": question["text"],
                "options": [
                    {
                        "id": option["id"],
                        **(
                            {"score": option["score"], "value": option["score"] * 25}
                            if scored
                            else {"value": 0}
                        ),
                        "label_i18n": option["label"],
                    }
                    for option in question["options"]
                ],
                "meta": meta,
                "weight": 1.0 if scored else 0.0,
                "is_active": True,
            }
        )
    return rows


def upgrade() -> None:
    # --- schema ------------------------------------------------------------
    op.add_column("assessment_questions", sa.Column("code", sa.String(length=40), nullable=True))
    op.add_column(
        "assessment_questions",
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.alter_column("assessment_questions", "dimension", nullable=True)
    op.create_index(
        "uq_assessment_questions_version_code",
        "assessment_questions",
        ["version", "code"],
        unique=True,
        postgresql_where=sa.text("code IS NOT NULL"),
    )

    op.add_column("assessments", sa.Column("overall_score", sa.Float(), nullable=True))
    op.add_column(
        "assessments",
        sa.Column("dimension_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "assessments",
        sa.Column(
            "goals",
            postgresql.ARRAY(sa.String(length=40)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
    )
    op.add_column("assessments", sa.Column("family_focus", sa.String(length=10), nullable=True))
    op.add_column(
        "assessments",
        sa.Column("priorities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("assessments", sa.Column("client_ref", sa.UUID(), nullable=True))
    op.create_index(
        "uq_assessments_user_client_ref",
        "assessments",
        ["user_id", "client_ref"],
        unique=True,
        postgresql_where=sa.text("client_ref IS NOT NULL"),
    )

    op.create_table(
        "score_adjustments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("assessment_id", sa.UUID(), nullable=False),
        sa.Column("dimension", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("points", sa.Float(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assessment_id",
            "dimension",
            "kind",
            "source_type",
            "source_id",
            name="uq_score_adjustments_source",
        ),
    )
    op.create_index(
        "ix_score_adjustments_user_dimension", "score_adjustments", ["user_id", "dimension"]
    )

    # --- data: the old dimensions become leadership --------------------------
    old = "('social_activity', 'international_integration')"
    op.execute(
        f"""
        INSERT INTO development_scores
            (id, user_id, assessment_id, dimension, baseline, current, target, measured_at,
             created_at, updated_at)
        SELECT gen_random_uuid(), user_id,
               (array_agg(assessment_id ORDER BY measured_at DESC NULLS LAST))[1],
               'leadership', avg(baseline), avg(current), avg(target), max(measured_at),
               now(), now()
        FROM development_scores
        WHERE dimension IN {old}
        GROUP BY user_id
        """
    )
    op.execute(f"DELETE FROM development_scores WHERE dimension IN {old}")
    for table in ("plan_items", "goals", "learning_paths", "assessment_questions"):
        op.execute(f"UPDATE {table} SET dimension = 'leadership' WHERE dimension IN {old}")
    op.execute(
        """
        UPDATE skills SET dimensions = ARRAY(
            SELECT DISTINCT CASE WHEN d IN ('social_activity', 'international_integration')
                                 THEN 'leadership' ELSE d END
            FROM unnest(dimensions) AS d
        )
        WHERE dimensions && ARRAY['social_activity', 'international_integration']::varchar[]
        """
    )
    op.execute(
        """
        UPDATE skills SET dimensions = array_append(dimensions, 'digital_skills')
        WHERE category = 'digital' AND NOT ('digital_skills' = ANY(dimensions))
        """
    )

    # --- the instrument -----------------------------------------------------
    op.execute("UPDATE assessment_questions SET is_active = false WHERE version < 2")
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
        "(SELECT id FROM assessment_questions WHERE version = 2)"
    )
    op.execute("DELETE FROM assessment_questions WHERE version = 2")
    op.execute("UPDATE assessment_questions SET is_active = true WHERE version = 1")
    op.execute("DELETE FROM development_scores WHERE dimension = 'digital_skills'")
    for table in ("plan_items", "goals", "learning_paths"):
        op.execute(f"UPDATE {table} SET dimension = NULL WHERE dimension = 'digital_skills'")
    for table in (
        "development_scores",
        "plan_items",
        "goals",
        "learning_paths",
        "assessment_questions",
    ):
        op.execute(
            f"UPDATE {table} SET dimension = 'social_activity' WHERE dimension = 'leadership'"
        )
    op.execute(
        """
        UPDATE skills SET dimensions = ARRAY(
            SELECT DISTINCT CASE WHEN d = 'leadership' THEN 'social_activity' ELSE d END
            FROM unnest(dimensions) AS d WHERE d <> 'digital_skills'
        )
        WHERE dimensions && ARRAY['leadership', 'digital_skills']::varchar[]
        """
    )

    op.drop_index("ix_score_adjustments_user_dimension", table_name="score_adjustments")
    op.drop_table("score_adjustments")
    op.drop_index("uq_assessments_user_client_ref", table_name="assessments")
    for column in (
        "client_ref",
        "priorities",
        "family_focus",
        "goals",
        "dimension_scores",
        "overall_score",
    ):
        op.drop_column("assessments", column)
    op.drop_index("uq_assessment_questions_version_code", table_name="assessment_questions")
    op.execute("DELETE FROM assessment_questions WHERE dimension IS NULL")
    op.alter_column("assessment_questions", "dimension", nullable=False)
    op.drop_column("assessment_questions", "meta")
    op.drop_column("assessment_questions", "code")
