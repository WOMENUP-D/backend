"""Small rules for the text people type into the portal."""

from __future__ import annotations

import re

_SPACES = re.compile(r"\s+")


def tidy_name(value: str | None) -> str | None:
    """A person's name written as a name, whatever case it was typed in.

    `durdona kim` becomes `Durdona Kim` and `DURDONA` becomes `Durdona`;
    `anna-maria` becomes `Anna-Maria`. A word that already mixes its cases
    (`McDonald`, `DeLong`, `Oʻtkir`) is left exactly as typed — somebody chose
    it. Letters after an apostrophe stay as they are, because in Uzbek names
    (`Gʻulom`, `Oʻtkir`, `Sa'dulla`) the apostrophe is part of a letter, not a
    break between words. Blank input comes back as None.
    """
    if value is None:
        return None
    text = _SPACES.sub(" ", value.strip())
    if not text:
        return None
    words = (("-".join(_tidy_part(part) for part in word.split("-"))) for word in text.split(" "))
    return " ".join(words)


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
