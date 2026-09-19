"""The skill taxonomy, the skills each woman holds, and the evidence behind them.

Skills were free text in four places — `profiles.skills`, `programs.skills_taught`,
`opportunities.required_skills` and `mentor_profiles.expertise` — compared by
lowercasing. That made "jamgʻarma" and "jamgarma" two different skills, left
every name untranslatable, and gave nothing to hang a level or a verification on.

Those columns stay exactly as they are: they hold the label an author or a
partner platform wrote, and rewriting an author's words under her is not this
migration's business. What is new is a vocabulary those labels resolve to, and
the two personal tables that let the platform say how well it knows a skill.

No data is moved here. `python -m app.seed_skills` loads the catalogue and
turns existing profiles, completed courses and certificates into evidence; it
is idempotent and safe to run repeatedly.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # The identifier everything else points at. Not the Uzbek word: a
        # display name gets reworded, an identifier must not move.
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name_i18n", postgresql.JSONB(), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False, server_default="professional"),
        sa.Column(
            "dimensions",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        sa.Column("is_curated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_skills"),
        sa.UniqueConstraint("slug", name="uq_skills_slug"),
    )
    op.create_index("ix_skills_slug", "skills", ["slug"])
    op.create_index("ix_skills_category_active", "skills", ["category", "is_active"])
    # Label lookup is an array-overlap on every match, which is what GIN answers.
    op.create_index("ix_skills_aliases", "skills", ["aliases"], postgresql_using="gin")

    op.create_table(
        "user_skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Both derived from the evidence rows below, never written by hand.
        # NULL means every piece of evidence for it has been withdrawn.
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("level", sa.String(length=20), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_skills_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skills.id"], name="fk_user_skills_skill_id_skills", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_skills"),
        sa.UniqueConstraint("user_id", "skill_id", name="uq_user_skills_user_id"),
    )
    op.create_index("ix_user_skills_user_id", "user_skills", ["user_id"])
    op.create_index("ix_user_skills_skill_id", "user_skills", ["skill_id"])
    # Employer matching will ask "who holds this skill, verified?" — one index
    # answers it without scanning a woman's whole profile.
    op.create_index("ix_user_skills_skill_status", "user_skills", ["skill_id", "status"])

    op.create_table(
        "skill_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_skill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        # The record that produced it, and the idempotency key with `kind`:
        # replaying a completion adds nothing.
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("verified_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        # Withdrawn, never deleted: a revoked certificate is part of the story.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_skill_id"],
            ["user_skills.id"],
            name="fk_skill_evidence_user_skill_id_user_skills",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["verified_by_id"],
            ["users.id"],
            name="fk_skill_evidence_verified_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_skill_evidence"),
        sa.UniqueConstraint(
            "user_skill_id",
            "kind",
            "source_type",
            "source_id",
            name="uq_skill_evidence_user_skill_id",
        ),
    )
    op.create_index("ix_skill_evidence_user_skill_id", "skill_evidence", ["user_skill_id"])


def downgrade() -> None:
    op.drop_index("ix_skill_evidence_user_skill_id", table_name="skill_evidence")
    op.drop_table("skill_evidence")
    op.drop_index("ix_user_skills_skill_status", table_name="user_skills")
    op.drop_index("ix_user_skills_skill_id", table_name="user_skills")
    op.drop_index("ix_user_skills_user_id", table_name="user_skills")
    op.drop_table("user_skills")
    op.drop_index("ix_skills_aliases", table_name="skills")
    op.drop_index("ix_skills_category_active", table_name="skills")
    op.drop_index("ix_skills_slug", table_name="skills")
    op.drop_table("skills")
