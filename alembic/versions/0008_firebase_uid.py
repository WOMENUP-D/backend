"""Google sign-in now goes through Firebase, so the stable id changes.

The column held Google's own `sub`. With Firebase in front, the identifier that
never moves is the Firebase uid, and that is what a sign-in matches on first.
Same column, same uniqueness guarantee, renamed to say what it holds.

A rename rather than a drop-and-add: it keeps the values of any account already
linked, and Postgres does it without rewriting the table.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "google_sub", new_column_name="firebase_uid")
    op.execute("ALTER INDEX ix_users_google_sub RENAME TO ix_users_firebase_uid")


def downgrade() -> None:
    op.execute("ALTER INDEX ix_users_firebase_uid RENAME TO ix_users_google_sub")
    op.alter_column("users", "firebase_uid", new_column_name="google_sub")
