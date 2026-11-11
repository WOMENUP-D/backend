"""Diagnostic v3: questions, attempts, the result and its history.

Every route is about the signed-in user and only her: an attempt id that is
not hers reads as not found, never as forbidden, so ids cannot be probed.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUserDep, DbSession, TrackerDep
from app.models.assessment import Assessment, DevelopmentScore
from app.models.program import Enrollment
from app.schemas.diagnostic import (
    DiagnosticAttemptIn,
    DiagnosticHistoryItem,
    DiagnosticOption,
    DiagnosticQuestion,
    DiagnosticQuestions,
    DiagnosticRecommendation,
    DiagnosticResult,
    DimensionResult,
    GapRead,
    PriorityRead,
    RecommendationSummary,
    RecommendedCourse,
    RoadmapItem,
    RoadmapStage,
)
from app.schemas.program import ProgramRead
from app.schemas.skill import SkillRef
from app.services import course_match, diagnostic, telemetry

router = APIRouter(prefix="/diagnostic", tags=["diagnostic"])


@router.get("/questions", response_model=DiagnosticQuestions)
async def questions(user: CurrentUserDep, session: DbSession) -> DiagnosticQuestions:
    """The active instrument, in the order it is asked."""
    rows = await diagnostic.active_questions(session)
    return DiagnosticQuestions(
        version=diagnostic.VERSION,
        questions=[
            DiagnosticQuestion(
                id=q.id,
                code=q.code or str(q.id),
                dimension=q.dimension,
                type=q.question_type,
                order_index=q.order_index,
                text_i18n=q.text_i18n,
                hint_i18n=(q.meta or {}).get("hint_i18n"),
                max_choices=int((q.meta or {}).get("max_choices") or 1),
                section=(q.meta or {}).get("section"),
                optional=bool((q.meta or {}).get("optional")),
                show_if=(q.meta or {}).get("show_if"),
                max_length=(q.meta or {}).get("max_length"),
                options=[
                    DiagnosticOption(id=o["id"], label_i18n=o.get("label_i18n") or {})
                    for o in q.options
                    if "id" in o
                ],
            )
            for q in rows
        ],
    )


@router.post("/attempt", response_model=DiagnosticResult, status_code=status.HTTP_201_CREATED)
async def submit_attempt(
    payload: DiagnosticAttemptIn,
    user: CurrentUserDep,
    session: DbSession,
    response: Response,
    actor: TrackerDep,
) -> DiagnosticResult:
    """Score a completed run on the server and store it as a new attempt.

    Earlier attempts are never overwritten. Resending the same `client_ref`
    returns the attempt it already made (200 instead of 201).
    """
    user_id = uuid.UUID(user.id)
    try:
        attempt, created = await diagnostic.submit(
            session,
            user_id=user_id,
            answers=[(a.question_id, a.option_ids) for a in payload.answers],
            client_ref=payload.client_ref,
            started_at=payload.started_at,
        )
    except diagnostic.DiagnosticError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": error.code, "message": error.detail, "items": error.items},
        ) from None
    if created:
        await telemetry.record_event(
            session,
            actor=actor,
            **telemetry.business(
                "development_test_completed",
                key=attempt.id,
                entity_type="assessment",
                entity_id=attempt.id,
                properties={"version": attempt.version},
            ),
        )
    try:
        await session.commit()
    except IntegrityError:
        # Two copies of the same run raced past the client_ref check; the
        # unique index let one through. Hand back that one.
        await session.rollback()
        attempt, created = await diagnostic.submit(
            session, user_id=user_id, answers=[], client_ref=payload.client_ref
        )
    if not created:
        response.status_code = status.HTTP_200_OK
    return await _result(session, user_id, attempt)


@router.get("/result", response_model=DiagnosticResult)
async def read_result(
    user: CurrentUserDep, session: DbSession, attempt_id: uuid.UUID | None = None
) -> DiagnosticResult:
    """Her latest result, or one earlier attempt of hers by id."""
    user_id = uuid.UUID(user.id)
    attempt = (
        await diagnostic.own_attempt(session, user_id, attempt_id)
        if attempt_id
        else await diagnostic.latest_attempt(session, user_id)
    )
    if attempt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No diagnostic result")
    return await _result(session, user_id, attempt)


@router.get("/history", response_model=list[DiagnosticHistoryItem])
async def read_history(user: CurrentUserDep, session: DbSession) -> list[DiagnosticHistoryItem]:
    """Every completed attempt, newest first."""
    return [
        DiagnosticHistoryItem(
            attempt_id=a.id,
            version=a.version,
            completed_at=a.completed_at,
            is_baseline=a.is_baseline,
            overall=round(a.overall_score) if a.overall_score is not None else None,
            dimensions={k: int(v) for k, v in (a.dimension_scores or {}).items()},
            goals=list(a.goals or []),
        )
        for a in await diagnostic.history(session, uuid.UUID(user.id))
    ]


async def _result(session: DbSession, user_id: uuid.UUID, attempt: Assessment) -> DiagnosticResult:
    scores = diagnostic.attempt_scores(attempt)
    current = {
        row.dimension: row.current
        for row in (
            await session.execute(
                select(DevelopmentScore).where(DevelopmentScore.user_id == user_id)
            )
        ).scalars()
    }
    overall = round(attempt.overall_score or 0)
    ranked = [p for p in diagnostic.stored_priorities(attempt) if p.eligible]
    growth = ranked[: diagnostic.TOP_PRIORITIES]
    return DiagnosticResult(
        attempt_id=attempt.id,
        version=attempt.version,
        completed_at=attempt.completed_at,
        is_baseline=attempt.is_baseline,
        overall=overall,
        level=diagnostic.level_for(overall),
        dimensions=[
            DimensionResult(
                dimension=dimension,
                score=value,
                current=current.get(dimension),
                level=diagnostic.level_for(value),
            )
            for dimension, value in scores.items()
        ],
        strongest=diagnostic.strongest(scores, exclude={p.dimension for p in growth}),
        growth=[
            PriorityRead(
                dimension=p.dimension,
                priority=p.priority,
                need=p.need,
                goal_match=p.goal_match,
                skill_gap=p.skill_gap,
                urgency=p.urgency,
            )
            for p in growth
        ],
        next_steps=await diagnostic.next_steps(session, user_id, attempt),
        goals=list(attempt.goals or []),
        family_focus=attempt.family_focus,
        profile=attempt.profile,
        support_suggested=await diagnostic.support_suggested(session, attempt),
    )


@router.get("/recommendations", response_model=DiagnosticRecommendation)
async def read_recommendations(
    user: CurrentUserDep, session: DbSession, attempt_id: uuid.UUID | None = None
) -> DiagnosticRecommendation:
    """The courses chosen for her from her latest attempt (or one of hers by
    id). Built once per attempt and kept on it; `refresh` rebuilds. The
    rules answer at once; the model's words follow from the worker while
    `ai_pending` is true."""
    user_id = uuid.UUID(user.id)
    attempt = await _attempt_of(session, user_id, attempt_id)
    if attempt.recommendation is None:
        await course_match.recommend(session, attempt)
        await session.commit()
    return await _recommendation(session, user_id, attempt)


@router.post("/recommendations/refresh", response_model=DiagnosticRecommendation)
async def refresh_recommendations(
    user: CurrentUserDep, session: DbSession, attempt_id: uuid.UUID | None = None
) -> DiagnosticRecommendation:
    """Build the recommendation again — after she enrolled, finished a course,
    or added skills to her profile. Counted as an expensive request, since
    it queues another turn of the model."""
    user_id = uuid.UUID(user.id)
    attempt = await _attempt_of(session, user_id, attempt_id)
    await course_match.recommend(session, attempt)
    await session.commit()
    return await _recommendation(session, user_id, attempt)


async def _attempt_of(
    session: DbSession, user_id: uuid.UUID, attempt_id: uuid.UUID | None
) -> Assessment:
    attempt = (
        await diagnostic.own_attempt(session, user_id, attempt_id)
        if attempt_id
        else await diagnostic.latest_attempt(session, user_id)
    )
    if attempt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No diagnostic result")
    return attempt


async def _recommendation(
    session: DbSession, user_id: uuid.UUID, attempt: Assessment
) -> DiagnosticRecommendation:
    stored = attempt.recommendation or {}
    programs = await course_match.programs_in(session, stored)
    enrolled = {
        str(e.program_id): e.status
        for e in (
            await session.execute(select(Enrollment).where(Enrollment.user_id == user_id))
        ).scalars()
    }
    gap_names = {g["slug"]: g.get("name_i18n") or {} for g in stored.get("gaps") or []}
    courses = []
    for item in stored.get("courses") or []:
        program = programs.get(item["program_id"])
        if program is None or not program.is_published:
            continue  # unpublished since: not offered, not named
        courses.append(
            RecommendedCourse(
                program=ProgramRead.model_validate(program),
                priority=item["priority"],
                why_i18n=item.get("why_i18n") or {},
                outcome_i18n=item.get("outcome_i18n") or {},
                covers=[
                    SkillRef(slug=slug, name_i18n=gap_names.get(slug) or {}, label=slug)
                    for slug in item.get("covers") or []
                ],
                enrollment_status=enrolled.get(item["program_id"]),
            )
        )
    roadmap = []
    for stage in stored.get("roadmap") or []:
        items = []
        for entry in stage.get("items") or []:
            if entry.get("kind") == "course":
                program = programs.get(entry.get("program_id") or "")
                if program is None or not program.is_published:
                    continue
                items.append(RoadmapItem(kind="course", program_id=program.id))
            else:
                items.append(
                    RoadmapItem(
                        kind="activity",
                        activity=entry.get("activity"),
                        text_i18n=entry.get("text_i18n") or {},
                    )
                )
        if items:
            roadmap.append(RoadmapStage(title_i18n=stage.get("title_i18n") or {}, items=items))
    return DiagnosticRecommendation(
        attempt_id=attempt.id,
        generated_at=stored.get("generated_at") or attempt.completed_at,
        mode=stored.get("mode") or "insufficient",
        summary=RecommendationSummary(**(stored.get("summary") or {})),
        courses=courses,
        roadmap=roadmap,
        next_steps=stored.get("next_steps") or [],
        follow_up=stored.get("follow_up"),
        gaps=[GapRead(**g) for g in stored.get("gaps") or []],
        ai_pending=bool(stored.get("ai_pending")),
    )
