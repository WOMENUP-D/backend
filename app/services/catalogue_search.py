"""What the catalogue search box matches, worked out once and tested on its own.

Ninety-odd per cent of the catalogue is written in Russian or English and the
portal's first language is Uzbek, so a search that only compared the typed
letters with the stored ones found "английский" 68 times and "ingliz tili"
twice. Three things close that gap without inventing anything:

* **Spellings.** The term as typed, in the reformed alphabet and in the old one
  ("tikuvçilik" and "tikuvchilik"), with ё folded into е ("ребёнок" finds
  "ребенок").
* **The taxonomy.** A term — or a word of it, with a common Uzbek suffix taken
  off ("salomatligi" → "salomatlik") — that is the name of a skill brings every
  name and alias of that skill with it, so "ingliz tili" also searches "английский" and
  "english". Only spellings the curated taxonomy already holds are added.
* **Literal matching.** `%` and `_` are characters, not wildcards; a term of one
  or two letters ("IT") matches whole words only, so "a" no longer finds every
  course with an "a" in it. A single character is not a search at all.

Typos ("pyton") are not handled: that needs the `pg_trgm` extension.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.program import Program
from app.models.skill import Skill
from app.services import skills
from app.services.uzbek import to_new_alphabet, to_old_alphabet

#: Shorter than this, a term is matched as a whole word, not as a substring.
WORD_ONLY_BELOW = 3
#: A word shorter than this is never stemmed or looked up in the taxonomy.
STEM_MIN = 4
#: Uzbek endings taken off a word before it is looked up, longest first. The
#: list is deliberately short: possessive and plural forms people type when
#: they search ("salomatligi", "kurslari", "tilini").
UZBEK_SUFFIXES = (
    "larimiz",
    "laringiz",
    "lari",
    "ligi",
    "lik",
    "lar",
    "ini",
    "ning",
    "dagi",
    "ga",
    "da",
    "ni",
    "si",
)
#: A stem that ended in -lig- before a vowel is the -lik form ("salomatligi").
_LIG = re.compile(r"lig$")
#: How many taxonomy spellings one search may expand into.
MAX_EXPANSIONS = 40


def fold(text: str) -> str:
    """Lower case with ё as е — the form both sides are compared in."""
    return text.casefold().replace("ё", "е")


def escape_like(term: str) -> str:
    """A LIKE pattern body that matches `term` literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def spellings(term: str) -> set[str]:
    """The term as typed, in the reformed and the old alphabet, folded."""
    bare = " ".join(term.split())
    forms = {bare, to_new_alphabet(bare), to_old_alphabet(to_new_alphabet(bare))}
    return {fold(form) for form in forms if form}


def stems(word: str) -> set[str]:
    """The word and its forms without one common Uzbek ending."""
    word = fold(word)
    out = {word}
    for suffix in UZBEK_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= STEM_MIN - 1:
            stem = word[: -len(suffix)]
            out.add(stem)
            if suffix.startswith("lig"):
                out.add(stem + "lik")  # salomat-ligi → salomatlik
            elif _LIG.search(stem):
                out.add(_LIG.sub("lik", stem))
    return out


def lookup_forms(search: str) -> set[str]:
    """Everything worth asking the taxonomy about: the whole term and each
    word of it, each in every spelling and without a common ending."""
    forms: set[str] = set()
    for spelling in spellings(search):
        forms.add(spelling)
        words = re.findall(r"[\w'ʻʼ‘’-]+", spelling)
        for word in words:
            if len(word) >= STEM_MIN:
                for stem in stems(word):
                    forms.update(spellings(stem))
        # The phrase with its last word unbent: "ingliz tilini" → "ingliz tili".
        if len(words) > 1 and len(words[-1]) >= STEM_MIN:
            head = " ".join(words[:-1])
            forms.update(f"{head} {stem}" for stem in stems(words[-1]))
    return {form for form in forms if form}


def skill_spellings(skill: Skill) -> set[str]:
    words = {skill.slug.replace("-", " "), *(str(n) for n in skill.name_i18n.values())}
    words.update(skill.aliases or [])
    return {fold(word) for word in words if word and len(word.strip()) >= WORD_ONLY_BELOW}


async def expand(session: AsyncSession, search: str) -> set[str]:
    """The folded terms a search matches: its own spellings plus the names of
    every taxonomy skill it, or one of its words, names exactly."""
    terms = spellings(search)
    forms = lookup_forms(search)
    keys = sorted({skills.normalise(form) for form in forms if form})
    if not keys:
        return terms
    rows = await session.execute(
        select(Skill).where(Skill.is_active.is_(True), Skill.aliases.overlap(keys))
    )
    wanted = set(keys)
    extra: set[str] = set()
    for skill in rows.scalars():
        # Only a term that *is* the skill — its slug or one of its names —
        # brings the other spellings. "python" is an alias of the broader
        # "programming"; searching it must not return every programming course.
        names = {skills.normalise(skill.slug.replace("-", " "))}
        names.update(skills.normalise(str(n)) for n in skill.name_i18n.values())
        if names & wanted:
            extra.update(skill_spellings(skill))
    return terms | set(sorted(extra)[:MAX_EXPANSIONS])


def _folded(column) -> ColumnElement:
    return func.translate(func.lower(column), "ё", "е")


def searchable_columns() -> list[ColumnElement]:
    """Title, goal and description in every locale, the tags and the slug."""
    columns: list[ColumnElement] = []
    for field in (Program.title_i18n, Program.goal_i18n, Program.description_i18n):
        for lang in ("uz", "ru", "en"):
            columns.append(_folded(field.op("->>")(lang)))
    columns.append(_folded(func.array_to_string(Program.skills_taught, " ")))
    columns.append(_folded(Program.slug))
    return columns


def word_regex(term: str) -> str:
    """A Postgres regex matching `term` as a whole word."""
    return r"\m" + re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", term) + r"\M"


def clause(terms: Iterable[str]) -> ColumnElement | None:
    """One OR over every column and every term; None when nothing is searchable."""
    columns = searchable_columns()
    parts: list[ColumnElement] = []
    for term in sorted(set(terms)):
        if len(term) < 2:
            continue
        if len(term) < WORD_ONLY_BELOW:
            regex = word_regex(term)
            parts.extend(column.op("~*")(regex) for column in columns)
        else:
            pattern = f"%{escape_like(term)}%"
            parts.extend(column.like(pattern, escape="\\") for column in columns)
    return or_(*parts) if parts else None


def is_searchable(search: str | None) -> bool:
    """Whether the box holds a search at all: two characters or more."""
    return bool(search) and len(search.strip()) >= 2
