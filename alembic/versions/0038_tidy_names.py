"""Names as names: capitalise what was typed in lower case.

A woman who typed `durdona kim` on her phone saw `durdona kim` on her card.
From now on the API tidies a name as it is saved (`app.core.text.tidy_name`);
this pass applies the same rule once to the names already stored. Only the
case changes, and only on words typed all in lower or all in upper case — a
word that mixes its cases was chosen that way and is left alone. The original
casing is not kept, so the downgrade has nothing to restore.

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

_SPACES = re.compile(r"\s+")


def _tidy_part(part: str) -> str:
    if not part:
        return part
    letters = [char for char in part if char.isalpha()]
    if not letters:
        return part
    if len(letters) > 1 and not (part.islower() or part.isupper()):
        return part
    first = next(index for index, char in enumerate(part) if char.isalpha())
    prefix, head, tail = part[:first], part[first], part[first + 1 :]
    return prefix + head.upper() + (tail.lower() if part.isupper() else tail)


def tidy_name(value: str) -> str | None:
    text = _SPACES.sub(" ", value.strip())
    if not text:
        return None
    words = (("-".join(_tidy_part(part) for part in word.split("-"))) for word in text.split(" "))
    return " ".join(words)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, full_name FROM profiles WHERE full_name IS NOT NULL")
    ).all()
    for row_id, name in rows:
        tidy = tidy_name(name)
        if tidy != name:
            bind.execute(
                sa.text("UPDATE profiles SET full_name = :name WHERE id = :id"),
                {"name": tidy, "id": row_id},
            )


def downgrade() -> None:
    pass
