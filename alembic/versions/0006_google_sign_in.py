"""Google sign-in: a stable subject id and how the account was opened.

`google_sub` is Google's own immutable identifier for the account. It is what a
sign-in matches on first, ahead of the e-mail address, because Google lets its
owner change the address and matching on the address alone would hand one woman
two WomanUP IDs the day she renames her mailbox. Unique, so the same Google
account can never be attached to two rows.

`auth_provider` records how the account was opened — for support and analytics
only. Authorisation is decided by roles, never by this column.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("google_sub", sa.String(length=255), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "auth_provider",
            sa.String(length=20),
            nullable=False,
            server_default="otp",
        ),
    )
    op.create_index(op.f("ix_users_google_sub"), "users", ["google_sub"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_google_sub"), table_name="users")
    op.drop_column("users", "auth_provider")
    op.drop_column("users", "google_sub")
