"""Personal directions: the fields she said interest her, as routes through
courses the catalogue really holds.

The diagnostic asks which fields interest her (`pro.q6`, up to three). Until
now that answer reached nothing on the learning page: the only routes there
are the ones an administrator authors by hand, and on a fresh platform there
are none. A direction is the missing link — a named field ("Kiberxavfsizlik",
"Maʼlumotlar tahlili") with the catalogue keys that select its courses.

Three rules keep it honest:

**Labels, not content.** A direction is a title, a one-line summary of the
field and a rule for picking courses. It promises nothing about any course;
the courses it lists are the published catalogue rows, shown as they are.

**No empty doors.** A direction is offered only when the catalogue holds at
least `min_courses` published courses for it, counted at the moment she asks.
A field the catalogue cannot serve yet is not shown as one it can.

**A reading, not a decision.** Nothing is written: no path is started, no
enrolment made. The page shows where her answers point; what she opens is
hers to choose. When her answers point nowhere the catalogue can follow
("Boşqa soha", or an older attempt with no interests), the fallback is said
out loud — directions for her lowest-scoring areas, or the best-stocked ones —
so a guess never reads as her own choice.

The content lives in `data/learning_directions.json`, read relative to the
working directory like the partner catalogue (`/app/data` in the image).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from sqlalchemy import exists, func, literal, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ProficiencyLevel, ProgramCategory, ScoreDimension
from app.models.program import Program
from app.services import catalogue_rules, diagnostic
from app.services.skills import normalise

logger = logging.getLogger(__name__)

#: Relative to the working directory — never derived from `__file__`, which in
#: the image points into the virtualenv's site-packages.
DIRECTIONS_FILE = Path("data/learning_directions.json")

#: Defaults when the file does not say.
MIN_COURSES = 3
MAX_COURSES = 8
#: How many directions a fallback offers.
FALLBACK_DIRECTIONS = 4
#: How many of her lowest-scoring areas the fallback looks at.
WEAKEST_DIMENSIONS = 2

#: Course order inside a direction: a stated level first, then what the
#: title says, then nothing known.
_LEVEL_STAGE = {
    ProficiencyLevel.BEGINNER: 0,
    ProficiencyLevel.ELEMENTARY: 0,
    ProficiencyLevel.INTERMEDIATE: 1,
    ProficiencyLevel.ADVANCED: 2,
    ProficiencyLevel.EXPERT: 2,
}
#: Titles that say the course is a first step. Used only to order courses
#: nobody has given a level; never shown as a level.
_FIRST_STEP = re.compile(
    r"с нуля|для начинающих|для новичков|введение|основы|базов|начальн|первые шаги|старт"
    r"|\bintro|beginner|basics|fundamentals|for everyone|from scratch|getting started"
    r"|boşlanğiç|boshlang|asoslari|\bчасть 1\b|\bpart 1\b",
    re.IGNORECASE,
)
_LATER_STEP = re.compile(
    r"\bчасть [2-9]\b|\bpart [2-9]\b|продолжени|углублен|\bpro\b|практикум|professional",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Direction:
    slug: str
    title_i18n: dict[str, str]
    summary_i18n: dict[str, str]
    interests: tuple[str, ...]
    dimensions: tuple[ScoreDimension, ...]
    #: Catalogue tags, folded, that select a course — a topic and, when the
    #: topic is the name of a taxonomy skill, every name of that skill.
    labels: frozenset[str]
    categories: frozenset[ProgramCategory] = frozenset()
    title_keywords: tuple[str, ...] = ()
    require_title: tuple[str, ...] = ()
    exclude_title: tuple[str, ...] = ()
    #: The topic the catalogue link opens with ("see every course").
    topic: str | None = None


@dataclass(frozen=True)
class DirectionCatalogue:
    min_courses: int
    max_courses: int
    interests: dict[str, dict[str, str]]
    directions: tuple[Direction, ...]

    def for_interest(self, interest: str) -> list[Direction]:
        """The directions of one interest: the ones that are its home (listed
        under it first) before the ones it shares with another field, each
        group in file order."""
        found = [d for d in self.directions if interest in d.interests]
        return sorted(found, key=lambda d: d.interests.index(interest) > 0)


# ---------------------------------------------------------------------------
# The content file
# ---------------------------------------------------------------------------


def topic_labels(topic: str) -> set[str]:
    """The folded tags a topic selects — the same reading as the catalogue's
    `?skill=` filter: a topic that *is* a taxonomy skill (its slug or one of
    its names) brings every name of that skill, so "english" also selects
    "английский язык"; an alias of a broader skill ("python" of programming)
    stays itself, so a Python direction does not swallow every C++ course."""
    from app.seed_skills import CATALOGUE

    key = normalise(topic)
    out = {key}
    for item in CATALOGUE:
        names = {normalise(item["slug"].replace("-", " ")), *(normalise(n) for n in item["names"])}
        if key in names or key == normalise(item["slug"]):
            out |= names | {normalise(item["slug"])}
    return {label for label in out if label}


def parse(raw: dict) -> DirectionCatalogue:
    """The file, checked. A direction that names an unknown dimension or
    category is a mistake in the file and refuses the whole file."""
    directions: list[Direction] = []
    seen: set[str] = set()
    for item in raw.get("directions", []):
        slug = str(item["slug"])
        if slug in seen:
            raise ValueError(f"direction {slug!r} is listed twice")
        seen.add(slug)
        topics = [str(t) for t in item.get("topics", [])]
        labels: set[str] = set()
        for topic in topics:
            labels |= topic_labels(topic)
        directions.append(
            Direction(
                slug=slug,
                title_i18n=dict(item["title"]),
                summary_i18n=dict(item["summary"]),
                interests=tuple(item.get("interests", [])),
                dimensions=tuple(ScoreDimension(d) for d in item.get("dimensions", [])),
                labels=frozenset(labels),
                categories=frozenset(ProgramCategory(c) for c in item.get("categories", [])),
                title_keywords=tuple(k.casefold() for k in item.get("title_keywords", [])),
                require_title=tuple(k.casefold() for k in item.get("require_title", [])),
                exclude_title=tuple(k.casefold() for k in item.get("exclude_title", [])),
                topic=topics[0] if topics else None,
            )
        )
    return DirectionCatalogue(
        min_courses=int(raw.get("min_courses", MIN_COURSES)),
        max_courses=int(raw.get("max_courses", MAX_COURSES)),
        interests={str(k): dict(v) for k, v in raw.get("interests", {}).items()},
        directions=tuple(directions),
    )


@lru_cache(maxsize=4)
def _load(path: Path) -> DirectionCatalogue:
    return parse(json.loads(path.read_text(encoding="utf-8")))


def load(path: Path = DIRECTIONS_FILE) -> DirectionCatalogue | None:
    """The shipped directions, or None — logged — when the file is missing or
    malformed. The page then shows the admin paths alone; it never fails."""
    try:
        return _load(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.error("learning directions unavailable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Which courses a direction selects, and in what order
# ---------------------------------------------------------------------------


def _title_has(title: str, words: tuple[str, ...]) -> bool:
    """Whether a keyword starts a word of the title, case-blind."""
    return any(re.search(r"(?<![\w])" + re.escape(word), title) for word in words)


def course_title(program) -> str:
    return " ".join(str(t) for t in (program.title_i18n or {}).values()).casefold()


def relevance(direction: Direction, program) -> int:
    """How many of the direction's keys the course meets; 0 when it is not one
    of the direction's courses."""
    title = course_title(program)
    if direction.exclude_title and _title_has(title, direction.exclude_title):
        return 0
    if direction.require_title and not _title_has(title, direction.require_title):
        return 0
    tags = {normalise(str(tag)) for tag in (program.skills_taught or [])}
    score = len(tags & direction.labels)
    if direction.categories and program.category in direction.categories:
        score += 1
    if direction.title_keywords and _title_has(title, direction.title_keywords):
        score += 1
    return score


def stage(program) -> int:
    """0 first step, 1 middle, 2 later. A stated level decides; otherwise the
    title's own words, and a course pitched at professionals goes last."""
    if program.level is not None:
        return _LEVEL_STAGE.get(ProficiencyLevel(program.level), 1)
    title = course_title(program)
    if catalogue_rules.professional_only(program) or _LATER_STEP.search(title):
        return 2
    if _FIRST_STEP.search(title):
        return 0
    return 1


def ordered(direction: Direction, programs, *, language: str | None = None) -> list:
    """The direction's courses, first steps first, each once.

    Ties break on how squarely the course sits in the direction, then on the
    language she reads the site in, then on the title — a stable order, so the
    list does not reshuffle between visits."""
    picked = {}
    for program in programs:
        score = relevance(direction, program)
        if score and program.id not in picked:
            picked[program.id] = (program, score)
    return [
        program
        for program, score in sorted(
            picked.values(),
            key=lambda pair: (
                stage(pair[0]),
                -pair[1],
                0 if language and _language(pair[0]) == language else 1,
                course_title(pair[0]),
                str(pair[0].id),
            ),
        )
    ]


def _language(program) -> str | None:
    value = program.language
    return getattr(value, "value", value)


def prefilter(directions: list[Direction]):
    """One SQL condition that every course of any of these directions meets.
    A superset: `relevance` makes the exact call in Python."""
    labels = sorted(set().union(*(d.labels for d in directions)))
    categories = sorted(set().union(*(d.categories for d in directions)))
    keywords = sorted({k for d in directions for k in d.title_keywords})
    parts = []
    if labels:
        # `unnest(skills_taught) AS t`: in Postgres the alias names the value.
        tags = func.unnest(Program.skills_taught).alias("t")
        parts.append(
            exists(
                select(literal(1))
                .select_from(tags)
                .where(func.lower(func.trim(literal_column("t"))).in_(labels))
            )
        )
    if categories:
        parts.append(Program.category.in_(categories))
    if keywords:
        titles = [Program.title_i18n.op("->>")(lang) for lang in ("uz", "ru", "en")]
        parts.extend(
            title.ilike(f"%{catalogue_search_escape(k)}%", escape="\\")
            for title in titles
            for k in keywords
        )
    return or_(*parts) if parts else None


def catalogue_search_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def candidates(session: AsyncSession, directions: list[Direction]) -> list[Program]:
    condition = prefilter(directions)
    if condition is None:
        return []
    rows = await session.execute(
        select(Program).where(Program.is_published.is_(True), condition).order_by(Program.id)
    )
    return list(rows.scalars())


# ---------------------------------------------------------------------------
# Her directions
# ---------------------------------------------------------------------------


@dataclass
class Picked:
    direction: Direction
    courses: list
    total: int
    #: "interest" | "weakest_dimension" | "popular"
    reason: str
    interest: str | None = None
    dimension: ScoreDimension | None = None


@dataclass
class ForHer:
    needs_diagnostic: bool
    #: "interests" when every direction comes from what she chose; otherwise
    #: the fallback that produced them. None when nothing could be offered.
    basis: str | None = None
    interests: list[str] = field(default_factory=list)
    unmatched_interests: list[str] = field(default_factory=list)
    picked: list[Picked] = field(default_factory=list)
    assessment_id: uuid.UUID | None = None


def weakest(scores: dict[ScoreDimension, int], limit: int = WEAKEST_DIMENSIONS):
    """Her lowest-scoring areas, canonical order breaking ties."""
    order = list(ScoreDimension)
    ranked = sorted(scores.items(), key=lambda kv: (kv[1], order.index(kv[0])))
    return [dimension for dimension, _ in ranked[:limit]]


def _stocked(
    catalogue: DirectionCatalogue,
    directions: list[Direction],
    programs: list,
    language: str | None,
) -> dict[str, list]:
    """Each direction's ordered courses — only the ones with enough of them."""
    out: dict[str, list] = {}
    for direction in directions:
        courses = ordered(direction, programs, language=language)
        if len(courses) >= catalogue.min_courses:
            out[direction.slug] = courses
    return out


async def for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    language: str | None = None,
    catalogue: DirectionCatalogue | None = None,
) -> ForHer:
    """Her directions, from her latest completed diagnostic. Reads only."""
    attempt = await diagnostic.latest_attempt(session, user_id)
    if attempt is None:
        return ForHer(needs_diagnostic=True)

    catalogue = catalogue or load()
    result = ForHer(needs_diagnostic=False, assessment_id=attempt.id)
    if catalogue is None:
        return result

    chosen = [str(i) for i in ((attempt.profile or {}).get("interests") or [])]
    result.interests = chosen

    everything = list(catalogue.directions)
    programs = await candidates(session, everything)
    stocked = _stocked(catalogue, everything, programs, language)
    cap = catalogue.max_courses

    def pick(direction: Direction, reason: str, **why) -> Picked:
        courses = stocked[direction.slug]
        return Picked(direction, courses[:cap], len(courses), reason, **why)

    shown: set[str] = set()
    for interest in chosen:
        found = [d for d in catalogue.for_interest(interest) if d.slug in stocked]
        if not found and interest != "other":
            result.unmatched_interests.append(interest)
        for direction in found:
            if direction.slug in shown:
                continue
            shown.add(direction.slug)
            result.picked.append(pick(direction, "interest", interest=interest))
    if result.picked:
        result.basis = "interests"
        return result

    # Nothing she chose leads anywhere the catalogue can follow: her lowest
    # areas, said as such.
    for dimension in weakest(diagnostic.attempt_scores(attempt)):
        for direction in sorted(
            (d for d in everything if dimension in d.dimensions and d.slug in stocked),
            key=lambda d: -len(stocked[d.slug]),
        ):
            if direction.slug in shown or len(result.picked) >= FALLBACK_DIRECTIONS:
                continue
            shown.add(direction.slug)
            result.picked.append(pick(direction, "weakest_dimension", dimension=dimension))
    if result.picked:
        result.basis = "weakest_dimension"
        return result

    for direction in sorted(
        (d for d in everything if d.slug in stocked), key=lambda d: -len(stocked[d.slug])
    )[:FALLBACK_DIRECTIONS]:
        result.picked.append(pick(direction, "popular"))
    result.basis = "popular" if result.picked else None
    return result


def coverage(catalogue: DirectionCatalogue, programs: list) -> dict[str, int]:
    """How many published courses each direction selects — the check the
    content file is held to (`tests/services/test_learning_directions.py`)."""
    published = [p for p in programs if getattr(p, "is_published", True)]
    return {d.slug: len(ordered(d, published)) for d in catalogue.directions}
