"""What the importer decides about a partner course on its own — written down.

The partner file (`data/partner_courses.json`) says what each platform says
about its course: a title, an address, a summary, a category picked when the
file was assembled and, for some, the skills it teaches. Three of those were
wrong often enough to break the catalogue (QA audit G-04, G-15, G-25, D-01),
and each is corrected here by an explicit rule rather than by hand-editing
eight hundred records:

* **Category.** Half the file was filed under digital *safety* — Python, SQL,
  C++, Figma. A course stays there only when its title is about security;
  every other digital course is digital *skills*. A short list of title rules
  moves the remaining misfiles (a café course under health, legal psychology
  under health). Everything else keeps the file's category.
* **Tags.** A course the file gives no skills gets the taxonomy skills its
  title names — whole words, Russian endings allowed — and nothing else. No
  summary mining: a summary that mentions Russian is not a Russian course.
* **What is not offered.** Exam preparation for another country's schools
  (ЕГЭ, ОГЭ, ГИА), archived and unfinished courses, and records the file marks
  `"available": false` (private, paid or a landing page, as checked against
  the platform) are imported unpublished: the row stays, so a woman already
  enrolled keeps her history, but the catalogue and the recommender skip it.

Every rule is a pure function of the record, so a run is deterministic and the
same file always gives the same catalogue.
"""

from __future__ import annotations

import re
from functools import lru_cache

from app.core.constants import ProgramCategory

#: Titles that are about keeping data, accounts and people safe online.
SECURITY = re.compile(
    r"кибер|cyber|хакер|hacker|информационн\w* безопасн|безопасност\w* (веб|сет|данн|информац)"
    r"|анализ безопасности|защит\w* (персональных )?данн|фишинг|phishing|мошенни|scam"
    r"|xavfsiz",
    re.IGNORECASE,
)

#: Title rules for courses the file filed somewhere they do not belong. First
#: match wins; a course no rule names keeps the file's category.
CATEGORY_RULES: tuple[tuple[re.Pattern[str], ProgramCategory], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), category)
    for pattern, category in (
        (r"заведени\w* общественного питания|ресторан|кафе\b", ProgramCategory.ENTREPRENEURSHIP),
        (r"юридическ|юриспруденц|гражданское право|\bправ[оа]\b", ProgramCategory.LEGAL_LITERACY),
        (r"техническ\w* состояни\w* автотранспорт", ProgramCategory.VOCATIONAL_SKILLS),
        (r"для школьников", ProgramCategory.VOCATIONAL_SKILLS),
        (
            r"компьютерн\w* коммуникаци|информационно-коммуникационн",
            ProgramCategory.DIGITAL_SKILLS,
        ),
    )
)

#: Not offered in Uzbekistan's catalogue: another country's school exams,
#: archived courses, courses their authors say are unfinished.
EXCLUDED = re.compile(
    r"(?<![а-яё])(?:ЕГЭ|ОГЭ|ГИА)(?![а-яё])|архивн\w* курс|дорабатыва", re.IGNORECASE
)


#: Courses pitched at people who already do the job — interview drills,
#: middle/senior tracks, "Excel for accountants" — and so not a first step for
#: someone who has not said she works at that level (QA audit D-01).
PROFESSIONAL_ONLY = re.compile(
    r"собеседовани|interview|\bmiddle\b|\bsenior\b|для профессионал|продвинут|advanced"
    r"|для (?:бухгалтер|юрист|врач|медик|фармацевт|нко|инженер|программист|аналитик"
    r"|административн|руководител)",
    re.IGNORECASE,
)


def professional_only(program) -> bool:
    """Whether a course's title pitches it at people already doing the job."""
    title = " ".join(str(t) for t in (program.title_i18n or {}).values())
    return PROFESSIONAL_ONLY.search(title) is not None


def category_for(filed: ProgramCategory, title: str) -> ProgramCategory:
    """The category a partner course is listed under."""
    if SECURITY.search(title):
        return ProgramCategory.DIGITAL_SAFETY
    if filed is ProgramCategory.DIGITAL_SAFETY:
        # Programming for schoolchildren and "rights in Linux" are still
        # digital skills: the title rules below are for the other categories.
        return ProgramCategory.DIGITAL_SKILLS
    for pattern, category in CATEGORY_RULES:
        if pattern.search(title):
            return category
    return filed


def exclusion_for(record: dict, title: str) -> str | None:
    """Why a course is imported unpublished, or None when it is offered."""
    if record.get("available") is False:
        return str(record.get("unavailable_reason") or "unavailable")
    if EXCLUDED.search(title):
        return "not_relevant"
    return None


# --- tags from the title -----------------------------------------------------

_WORD = re.compile(r"[\w+#]+")
_CYRILLIC = re.compile("[а-яё]")
#: How many letters a Russian word may add to the stem of a taxonomy word —
#: "программирования" for "программирование", "английского" for "английский".
_ENDING = 3


def _stem(word: str) -> str:
    if _CYRILLIC.search(word) and len(word) > 5:
        return word[:-2]
    return word


def _matches(token: str, stem: str) -> bool:
    if _CYRILLIC.search(stem):
        return token.startswith(stem) and len(token) - len(stem) <= _ENDING
    # Latin words match whole, or with a plural s: "word" is not "wordpress".
    return token == stem or token == stem + "s"


@lru_cache(maxsize=1)
def _vocabulary() -> tuple[tuple[tuple[str, ...], str], ...]:
    """Every spelling of every curated skill, as word stems, with its slug."""
    from app.seed_skills import CATALOGUE
    from app.services.skills import normalise

    out: list[tuple[tuple[str, ...], str]] = []
    for item in CATALOGUE:
        spellings = {normalise(str(x)) for x in (*item["aliases"], *item["names"])}
        for spelling in sorted(spellings):
            words = tuple(_stem(w) for w in _WORD.findall(spelling))
            # One- and two-letter spellings ("ai", "it", "ml") are too
            # ambiguous to find inside a title.
            if not words or (len(words) == 1 and len(words[0]) < 3):
                continue
            out.append((words, item["slug"]))
    return tuple(out)


def tags_for(title: str, limit: int = 8) -> list[str]:
    """The taxonomy slugs the title names, in catalogue order."""
    from app.services.skills import normalise

    tokens = _WORD.findall(normalise(title))
    found: list[str] = []
    for words, slug in _vocabulary():
        if slug in found:
            continue
        n = len(words)
        for start in range(len(tokens) - n + 1):
            if all(_matches(tokens[start + k], words[k]) for k in range(n)):
                found.append(slug)
                break
    return found[:limit]
