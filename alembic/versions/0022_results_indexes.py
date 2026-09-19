"""Results dashboard: indexes on the timestamps its periods filter on.

Additive: indexes only, no data change. Each admin Results figure counts rows
whose own event time falls in a period — a registration on `users.created_at`,
a completion on `enrollments.completed_at` — and these columns had no index of
their own.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_users_created_at", "users", "created_at"),
    ("ix_assessments_completed_at", "assessments", "completed_at"),
    ("ix_enrollments_completed_at", "enrollments", "completed_at"),
    ("ix_certificates_issued_at", "certificates", "issued_at"),
    ("ix_skill_evidence_created_at", "skill_evidence", "created_at"),
    ("ix_outcome_records_created_at", "outcome_records", "created_at"),
    ("ix_task_attempts_evaluated_at", "task_attempts", "evaluated_at"),
    ("ix_user_learning_paths_completed_at", "user_learning_paths", "completed_at"),
)


def upgrade() -> None:
    for name, table, column in INDEXES:
        op.create_index(name, table, [column])


def downgrade() -> None:
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
