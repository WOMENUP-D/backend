"""Diagnostic v2: the 25-question instrument, its scoring and its priorities.

The flow, every step on the server:

    answers (question id + option id, nothing else)
      → validated against the active question set
      → dimension scores: (q1 + q2 + q3) / 12 * 100, rounded, within 0-100
      → overall score: the mean of the eight, rounded
      → priorities: 0.4 need + 0.3 goal match + 0.2 skill gap + 0.1 urgency
      → three next steps grounded in the real catalogue

The browser never sends a score, a priority or a user id. It sends which option
she chose; what that option is worth is read from the question row.

Wording is the cabinet's business, not this module's: levels and reasons go out
as keys, and a low score is "your current development point", never a verdict.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    NextStepKind,
    ProgramCategory,
    RecommendationReason,
    ScoreDimension,
)
from app.models.assessment import Assessment, AssessmentAnswer, AssessmentQuestion
from app.models.career import EducationEntry, WorkExperience
from app.models.skill import Skill
from app.schemas.recommendation import NextStep
from app.services import recommendation as engine
from app.services import skills as skill_service
from app.services.scoring import is_baseline_assessment, persist_scores

VERSION = 2
DATA = Path(__file__).resolve().parents[2] / "data" / "diagnostic_v2.json"

SCORED = "single_choice"
ROUTING = "routing"
GOALS = "goals"

D = ScoreDimension
_CANONICAL = list(ScoreDimension)

# --- goals (Q25) ---------------------------------------------------------------

#: The dimension each goal names outright. GoalMatch = 100.
GOAL_DIRECT: dict[str, tuple[ScoreDimension, ...]] = {
    "learn_skills": (D.EDUCATION_SKILLS,),
    "find_job": (D.EMPLOYMENT,),
    "grow_career": (D.EMPLOYMENT,),
    "digital_ai": (D.DIGITAL_SKILLS,),
    "finances": (D.FINANCIAL_LITERACY,),
    "business": (D.ENTREPRENEURSHIP,),
    "personal_skills": (D.EDUCATION_SKILLS,),
    "wellbeing": (D.HEALTHY_LIFESTYLE,),
    "family": (D.FAMILY_PARENTING,),
    "international": (D.LEADERSHIP,),
    "leadership": (D.LEADERSHIP,),
}
#: Dimensions a goal leans on without naming them. GoalMatch = 50.
GOAL_INDIRECT: dict[str, tuple[ScoreDimension, ...]] = {
    "learn_skills": (D.EMPLOYMENT, D.DIGITAL_SKILLS),
    "find_job": (D.EDUCATION_SKILLS, D.DIGITAL_SKILLS),
    "grow_career": (D.EDUCATION_SKILLS, D.LEADERSHIP),
    "digital_ai": (D.EDUCATION_SKILLS, D.EMPLOYMENT),
    "finances": (D.ENTREPRENEURSHIP, D.EMPLOYMENT),
    "business": (D.FINANCIAL_LITERACY, D.DIGITAL_SKILLS),
    "personal_skills": (D.LEADERSHIP, D.HEALTHY_LIFESTYLE),
    "wellbeing": (D.FAMILY_PARENTING,),
    "family": (D.HEALTHY_LIFESTYLE,),
    "international": (D.EDUCATION_SKILLS, D.EMPLOYMENT),
    "leadership": (D.EMPLOYMENT, D.ENTREPRENEURSHIP),
}

# --- urgency -------------------------------------------------------------------

NEUTRAL = 50.0
#: Urgency read from explicit current-status answers, nowhere else. Health and
#: family are deliberately absent: the instrument asks nothing medical, and a
#: sensitive area is never made "urgent" by inference.
URGENCY_ANSWERS: dict[ScoreDimension, tuple[str, dict[str, float]]] = {
    # "Not working and no direction" is the most pressing career situation.
    D.EMPLOYMENT: ("car.q4", {"a": 100.0, "b": 75.0}),
    # Spending everything leaves no buffer.
    D.FINANCIAL_LITERACY: ("fin.q11", {"a": 75.0}),
}

#: Q19 options. "f" — the topic is not relevant to her right now.
FAMILY_NOT_RELEVANT = "f"
#: Which family programmes to put first for the topic she chose.
FAMILY_CATEGORIES: dict[str, tuple[ProgramCategory, ...]] = {
    "d": (ProgramCategory.PARENTING, ProgramCategory.ETHICS_CULTURE),
}
FAMILY_DEFAULT = (ProgramCategory.ETHICS_CULTURE, ProgramCategory.PARENTING)

WEIGHTS = {"need": 0.4, "goal_match": 0.3, "skill_gap": 0.2, "urgency": 0.1}
TOP_PRIORITIES = 3
STRONGEST = 2
#: A career priority starts from her CV when there is none yet, or when she
#: said she has none (Q5 "don't know where to start" / "not prepared yet").
CV_MISSING_ANSWERS = {"a", "b"}


class DiagnosticError(ValueError):
    """An attempt the server refuses. `code` is what the client can act on."""

    def __init__(self, code: str, detail: str, items: list[str] | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.items = items or []


def round_half_up(value: float) -> int:
    """School rounding: 62.5 → 63. Python's round() would give 62."""
    return math.floor(value + 0.5)


def load_bank(path: Path = DATA) -> dict:
    """The instrument as authored — for the seed and the tests. The API reads
    the question rows in the database, which the migration loaded from this."""
    return json.loads(path.read_text(encoding="utf-8"))


def question_rows(bank: dict) -> list[dict]:
    """The authored instrument as `AssessmentQuestion` fields — what migration
    0028 inserted, for the seed to insert the same way."""
    rows = []
    for index, question in enumerate(bank["questions"], start=1):
        scored = question["type"] == SCORED
        meta = {}
        if question.get("hint"):
            meta["hint_i18n"] = question["hint"]
        if question.get("max_choices"):
            meta["max_choices"] = question["max_choices"]
        rows.append(
            {
                "version": bank["version"],
                "code": question["code"],
                "dimension": ScoreDimension(question["dimension"])
                if question["dimension"]
                else None,
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


# --- the question set ------------------------------------------------------------


async def active_questions(session: AsyncSession) -> list[AssessmentQuestion]:
    rows = await session.execute(
        select(AssessmentQuestion)
        .where(AssessmentQuestion.version == VERSION, AssessmentQuestion.is_active.is_(True))
        .order_by(AssessmentQuestion.order_index)
    )
    return list(rows.scalars())


def _option_ids(question: AssessmentQuestion) -> list[str]:
    return [str(option["id"]) for option in question.options if "id" in option]


def _option(question: AssessmentQuestion, option_id: str) -> dict:
    return next(option for option in question.options if option.get("id") == option_id)


# --- validation ------------------------------------------------------------------


@dataclass(slots=True)
class Submission:
    """A validated attempt: the options chosen, by question."""

    questions: list[AssessmentQuestion]
    chosen: dict[uuid.UUID, list[str]]

    def by_code(self) -> dict[str, list[str]]:
        return {q.code: self.chosen[q.id] for q in self.questions if q.code and q.id in self.chosen}


def validate(
    questions: list[AssessmentQuestion], answers: list[tuple[uuid.UUID, list[str]]]
) -> Submission:
    """Every active question answered once, with options it actually has."""
    if not questions:
        raise DiagnosticError("no_questions", "The diagnostic is not available")
    known = {question.id: question for question in questions}

    chosen: dict[uuid.UUID, list[str]] = {}
    for question_id, option_ids in answers:
        question = known.get(question_id)
        if question is None:
            raise DiagnosticError("invalid_question", "Unknown question", [str(question_id)])
        if question_id in chosen:
            raise DiagnosticError(
                "duplicate_answer", "A question was answered twice", [question.code or ""]
            )
        allowed = set(_option_ids(question))
        ids = list(dict.fromkeys(option_ids))
        if len(ids) != len(option_ids) or not ids or any(i not in allowed for i in ids):
            raise DiagnosticError(
                "invalid_answer", "Not an option of this question", [question.code or ""]
            )
        if question.question_type == GOALS:
            limit = int((question.meta or {}).get("max_choices") or 3)
            if len(ids) > limit:
                raise DiagnosticError(
                    "too_many_goals", f"Choose at most {limit}", [question.code or ""]
                )
        elif len(ids) != 1:
            raise DiagnosticError(
                "invalid_answer", "Choose exactly one option", [question.code or ""]
            )
        chosen[question_id] = ids

    missing = [
        question.code or str(question.id) for question in questions if question.id not in chosen
    ]
    if missing:
        raise DiagnosticError("incomplete", "Some questions are unanswered", missing)
    return Submission(questions=questions, chosen=chosen)


# --- scoring ---------------------------------------------------------------------


def dimension_scores(submission: Submission) -> dict[ScoreDimension, int]:
    """(sum of the dimension's answers) / (4 × questions) × 100, rounded, 0-100."""
    totals: dict[ScoreDimension, int] = {}
    counts: dict[ScoreDimension, int] = {}
    for question in submission.questions:
        if question.question_type != SCORED or question.dimension is None:
            continue
        score = int(_option(question, submission.chosen[question.id][0])["score"])
        totals[question.dimension] = totals.get(question.dimension, 0) + score
        counts[question.dimension] = counts.get(question.dimension, 0) + 1
    return {
        dimension: max(
            0, min(100, round_half_up(totals[dimension] / (4 * counts[dimension]) * 100))
        )
        for dimension in _CANONICAL
        if counts.get(dimension)
    }


def overall_score(scores: dict[ScoreDimension, int]) -> int:
    """(EDU + CAR + ENT + FIN + DIG + HEA + FAM + LEA) / 8, rounded."""
    if not scores:
        return 0
    return max(0, min(100, round_half_up(sum(scores.values()) / len(scores))))


LEVELS = (
    (90, "advanced"),
    (75, "strong_area"),
    (50, "good_foundation"),
    (25, "building_foundation"),
    (0, "starting_point"),
)


def level_for(score: float) -> str:
    """The words for a score — a place to start from, never a grade."""
    return next(key for floor, key in LEVELS if score >= floor)


# --- priorities ------------------------------------------------------------------


def goal_match(dimension: ScoreDimension, goals: list[str]) -> float:
    if any(dimension in GOAL_DIRECT.get(goal, ()) for goal in goals):
        return 100.0
    if any(dimension in GOAL_INDIRECT.get(goal, ()) for goal in goals):
        return 50.0
    return 0.0


def urgency(dimension: ScoreDimension, by_code: dict[str, list[str]]) -> float:
    rule = URGENCY_ANSWERS.get(dimension)
    if rule is None:
        return NEUTRAL
    code, values = rule
    chosen = (by_code.get(code) or [None])[0]
    return values.get(chosen, NEUTRAL) if chosen else NEUTRAL


@dataclass(slots=True)
class Priority:
    dimension: ScoreDimension
    priority: float
    need: float
    goal_match: float
    skill_gap: float
    urgency: float
    # False when she said the area is not relevant to her right now.
    eligible: bool = True

    def as_dict(self) -> dict:
        return {
            "dimension": self.dimension.value,
            "priority": self.priority,
            "need": self.need,
            "goal_match": self.goal_match,
            "skill_gap": self.skill_gap,
            "urgency": self.urgency,
            "eligible": self.eligible,
        }


def priorities(
    scores: dict[ScoreDimension, int],
    goals: list[str],
    by_code: dict[str, list[str]],
    skill_gaps: dict[ScoreDimension, float],
    family_focus: str | None,
) -> list[Priority]:
    """Every dimension ranked by 0.4·Need + 0.3·GoalMatch + 0.2·SkillGap + 0.1·Urgency.

    Need alone would always point at the lowest score; the goal and urgency
    terms are what make this her ranking rather than a sort.
    """
    ranked = []
    for dimension in _CANONICAL:
        if dimension not in scores:
            continue
        need = float(100 - scores[dimension])
        match = goal_match(dimension, goals)
        gap = skill_gaps.get(dimension, NEUTRAL)
        urgent = urgency(dimension, by_code)
        value = (
            WEIGHTS["need"] * need
            + WEIGHTS["goal_match"] * match
            + WEIGHTS["skill_gap"] * gap
            + WEIGHTS["urgency"] * urgent
        )
        eligible = not (
            dimension == D.FAMILY_PARENTING and family_focus == FAMILY_NOT_RELEVANT and match < 100
        )
        ranked.append(
            Priority(dimension, round(value, 1), need, match, round(gap, 1), urgent, eligible)
        )
    return sorted(
        ranked, key=lambda p: (not p.eligible, -p.priority, _CANONICAL.index(p.dimension))
    )


async def skill_gaps(session: AsyncSession, user_id: uuid.UUID) -> dict[ScoreDimension, float]:
    """SkillGap per dimension from the skill system: the share of the platform's
    curated skills for the dimension that she does not yet hold.

    With no skill on record at all there is nothing to read a gap from, and
    every dimension gets the neutral 50 rather than a fabricated 100.
    """
    held = await skill_service.skill_keys(session, user_id)
    if not held:
        return {}
    rows = await session.execute(
        select(Skill.slug, Skill.dimensions).where(
            Skill.is_active.is_(True), Skill.is_curated.is_(True)
        )
    )
    relevant: dict[ScoreDimension, set[str]] = {}
    for slug, dimensions in rows:
        for value in dimensions or []:
            try:
                relevant.setdefault(ScoreDimension(value), set()).add(slug)
            except ValueError:
                continue
    return {
        dimension: 100.0 * (1 - len(slugs & held) / len(slugs))
        for dimension, slugs in relevant.items()
        if slugs
    }


# --- the attempt -----------------------------------------------------------------


async def submit(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    answers: list[tuple[uuid.UUID, list[str]]],
    client_ref: uuid.UUID | None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> tuple[Assessment, bool]:
    """Score and store one attempt. Returns (attempt, created).

    `completed_at` is for the demo seed only, which backdates attempts; the
    API never passes it.

    A repeat of an attempt already stored — the same `client_ref` — returns
    that attempt untouched, so a double tap never produces two results.
    """
    if client_ref is not None:
        existing = await session.scalar(
            select(Assessment).where(
                Assessment.user_id == user_id, Assessment.client_ref == client_ref
            )
        )
        if existing is not None:
            return existing, False

    questions = await active_questions(session)
    submission = validate(questions, answers)
    scores = dimension_scores(submission)
    overall = overall_score(scores)
    by_code = submission.by_code()

    goals_question = next((q for q in questions if q.question_type == GOALS), None)
    routing_question = next((q for q in questions if q.question_type == ROUTING), None)
    goals = submission.chosen.get(goals_question.id, []) if goals_question else []
    family_focus = (
        (submission.chosen.get(routing_question.id) or [None])[0] if routing_question else None
    )

    gaps = await skill_gaps(session, user_id)
    ranked = priorities(scores, goals, by_code, gaps, family_focus)

    now = completed_at or datetime.now(UTC)
    if started_at is None or started_at > now or (now - started_at).days > 1:
        started_at = now
    attempt = Assessment(
        user_id=user_id,
        version=VERSION,
        started_at=started_at,
        completed_at=now,
        is_baseline=await is_baseline_assessment(session, user_id),
        overall_score=float(overall),
        dimension_scores={dimension.value: value for dimension, value in scores.items()},
        goals=goals,
        family_focus=family_focus,
        priorities=[p.as_dict() for p in ranked],
        client_ref=client_ref,
    )
    session.add(attempt)
    await session.flush()

    for question in questions:
        ids = submission.chosen[question.id]
        value = _option(question, ids[0]).get("value", 0) if question.question_type == SCORED else 0
        session.add(
            AssessmentAnswer(
                assessment_id=attempt.id,
                question_id=question.id,
                value=float(value),
                raw_answer=",".join(ids),
            )
        )
    await persist_scores(
        session, user_id, attempt.id, {dimension: float(v) for dimension, v in scores.items()}
    )
    await session.flush()
    return attempt, True


async def latest_attempt(session: AsyncSession, user_id: uuid.UUID) -> Assessment | None:
    """Her most recent completed v2 attempt — the current baseline."""
    return await session.scalar(
        select(Assessment)
        .where(
            Assessment.user_id == user_id,
            Assessment.completed_at.is_not(None),
            Assessment.overall_score.is_not(None),
        )
        .order_by(Assessment.completed_at.desc())
        .limit(1)
    )


async def own_attempt(
    session: AsyncSession, user_id: uuid.UUID, attempt_id: uuid.UUID
) -> Assessment | None:
    """An attempt, only if it is hers — anyone else's reads as not found."""
    return await session.scalar(
        select(Assessment).where(
            Assessment.id == attempt_id,
            Assessment.user_id == user_id,
            Assessment.overall_score.is_not(None),
        )
    )


async def history(session: AsyncSession, user_id: uuid.UUID) -> list[Assessment]:
    rows = await session.execute(
        select(Assessment)
        .where(Assessment.user_id == user_id, Assessment.completed_at.is_not(None))
        .order_by(Assessment.completed_at.desc())
    )
    return list(rows.scalars())


# --- the result ------------------------------------------------------------------


def stored_priorities(attempt: Assessment) -> list[Priority]:
    out = []
    for row in attempt.priorities or []:
        try:
            dimension = ScoreDimension(row["dimension"])
        except (KeyError, ValueError):
            continue
        out.append(
            Priority(
                dimension,
                float(row.get("priority", 0)),
                float(row.get("need", 0)),
                float(row.get("goal_match", 0)),
                float(row.get("skill_gap", NEUTRAL)),
                float(row.get("urgency", NEUTRAL)),
                bool(row.get("eligible", True)),
            )
        )
    return out


def attempt_scores(attempt: Assessment) -> dict[ScoreDimension, int]:
    scores = {}
    for key, value in (attempt.dimension_scores or {}).items():
        try:
            scores[ScoreDimension(key)] = int(value)
        except ValueError:
            continue
    return dict(sorted(scores.items(), key=lambda kv: _CANONICAL.index(kv[0])))


def strongest(
    scores: dict[ScoreDimension, int],
    limit: int = STRONGEST,
    exclude: frozenset[ScoreDimension] | set[ScoreDimension] = frozenset(),
) -> list[ScoreDimension]:
    """Her highest dimensions, leaving out the ones offered as growth areas — an
    area cannot be both her strength and what she should work on next, which
    is what equal scores would otherwise produce."""
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], _CANONICAL.index(kv[0])))
    return [d for d, _ in ranked if d not in exclude][:limit]


@dataclass(slots=True)
class _Picked:
    steps: list[NextStep] = field(default_factory=list)
    program_ids: set = field(default_factory=set)


async def _has_cv(session: AsyncSession, user_id: uuid.UUID) -> bool:
    for model in (WorkExperience, EducationEntry):
        n = await session.scalar(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        )
        if n:
            return True
    return False


def _family_programs(ctx: engine.LearnerContext, family_focus: str | None):
    """Family courses ordered by the topic she chose in Q19."""
    order = FAMILY_CATEGORIES.get(family_focus or "", FAMILY_DEFAULT)
    programs = engine.programs_for_dimension(ctx, D.FAMILY_PARENTING)
    return sorted(
        programs, key=lambda p: order.index(p.category) if p.category in order else len(order)
    )


async def next_steps(
    session: AsyncSession, user_id: uuid.UUID, attempt: Assessment
) -> list[NextStep]:
    """One concrete step for each of her top priority areas, from the real
    catalogue: a course she is in, a route or a course to start, listings to
    act on — or, for a career with no CV yet, the CV. Where the platform has
    nothing for an area, the step is to explore its skills; nothing is made up.
    """
    ranked = [p for p in stored_priorities(attempt) if p.eligible][:TOP_PRIORITIES]
    if not ranked:
        return []
    ctx = await engine.load_context(session, user_id)
    scores = attempt_scores(attempt)
    by_code: dict[str, list[str]] = {}
    if any(p.dimension == D.EMPLOYMENT for p in ranked):
        rows = await session.execute(
            select(AssessmentQuestion.code, AssessmentAnswer.raw_answer)
            .join(AssessmentQuestion, AssessmentAnswer.question_id == AssessmentQuestion.id)
            .where(AssessmentAnswer.assessment_id == attempt.id)
        )
        by_code = {code: (raw or "").split(",") for code, raw in rows if code}

    picked = _Picked()
    for item in ranked:
        dimension = item.dimension
        params = {"score": scores.get(dimension, 0), "priority": round(item.priority)}
        step: NextStep | None = None

        if dimension == D.EMPLOYMENT and (
            (by_code.get("car.q5") or [""])[0] in CV_MISSING_ANSWERS
            or not await _has_cv(session, user_id)
        ):
            step = NextStep(
                kind=NextStepKind.BUILD_CV,
                reason=RecommendationReason.DIAGNOSTIC_PRIORITY,
                dimension=dimension,
                params=params,
            )

        if step is None and dimension == D.FAMILY_PARENTING:
            fresh = next(
                (
                    p
                    for p in _family_programs(ctx, attempt.family_focus)
                    if p.id not in ctx.enrollments and p.id not in picked.program_ids
                ),
                None,
            )
            if fresh is not None:
                step = engine._start_step(ctx, fresh, dimension)

        if step is None:
            for action in engine.dimension_actions(ctx, dimension):
                program = action.program
                if program is not None and program.id in picked.program_ids:
                    continue
                step = action
                break

        if step is None:
            step = NextStep(
                kind=NextStepKind.EXPLORE_SKILLS,
                reason=RecommendationReason.DIAGNOSTIC_PRIORITY,
                dimension=dimension,
                params=params,
            )

        if step.program is not None:
            picked.program_ids.add(step.program.id)
        step = step.model_copy(
            update={
                "reason": RecommendationReason.DIAGNOSTIC_PRIORITY,
                "params": {**(step.params or {}), **params},
            }
        )
        picked.steps.append(step)
    return picked.steps
