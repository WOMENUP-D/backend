"""AI Navigator and knowledge base management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import ContentDep, CurrentUserDep, DbSession, StaffDep
from app.models.assessment import DevelopmentScore
from app.models.audit import RiskFlag
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.plan import DevelopmentPlan
from app.schemas.ai import (
    KnowledgeDocumentCreate,
    KnowledgeDocumentRead,
    NavigatorAnswer,
    NavigatorQuery,
    NextStepCard,
    RiskFlagRead,
)
from app.schemas.common import Message
from app.services import rag
from app.services.ai_navigator import answer as navigator_answer

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/navigator", response_model=NavigatorAnswer)
async def ask_navigator(
    payload: NavigatorQuery, user: CurrentUserDep, session: DbSession
) -> NavigatorAnswer:
    """Ask the WomanUP Navigator.

    Answers come only from approved sources. High-risk topics are routed to a
    human without reaching the model.
    """
    return await navigator_answer(
        session,
        question=payload.message,
        user_id=uuid.UUID(user.id),
        language=payload.language or user.language.value,
        conversation_id=payload.conversation_id,
    )


@router.get("/next-step", response_model=NextStepCard)
async def next_step(user: CurrentUserDep, session: DbSession) -> NextStepCard:
    """The "Bugungi qadam" card: the single most useful action right now.

    Resolved from the active plan without calling the model — this card is on
    the home screen and must be fast and predictable.
    """
    from app.core.constants import PlanItemStatus

    plan = await session.scalar(
        select(DevelopmentPlan).where(
            DevelopmentPlan.user_id == uuid.UUID(user.id),
            DevelopmentPlan.is_active.is_(True),
        )
    )
    if plan is not None:
        pending = [i for i in plan.items if i.status != PlanItemStatus.DONE]
        if pending:
            item = min(pending, key=lambda i: (i.due_date or datetime.max.date(), i.order_index))
            return NextStepCard(
                title=item.action,
                description=item.description or "Rejangizdagi keyingi qadam.",
                action_url=f"/plans/items/{item.id}",
                dimension=item.dimension.value if item.dimension else None,
            )

    has_score = await session.scalar(
        select(DevelopmentScore.id).where(DevelopmentScore.user_id == uuid.UUID(user.id)).limit(1)
    )
    if not has_score:
        return NextStepCard(
            title="Diagnostikadan o‘ting",
            description="10–15 daqiqada shaxsiy rivojlanish yo‘lingizni aniqlaymiz.",
            action_url="/assessments/questions",
        )

    return NextStepCard(
        title="Rivojlanish rejangizni yarating",
        description="Diagnostika natijasi asosida individual reja tuzamiz.",
        action_url="/plans/generate",
    )


@router.post(
    "/knowledge",
    response_model=KnowledgeDocumentRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_knowledge_document(
    payload: KnowledgeDocumentCreate, user: ContentDep, session: DbSession
) -> KnowledgeDocument:
    """Add a document to the knowledge base.

    It is chunked immediately but stays unapproved — and therefore
    unretrievable — until a moderator approves it.
    """
    document = KnowledgeDocument(
        title=payload.title,
        source_type=payload.source_type,
        source_url=payload.source_url,
        language=payload.language,
        tags=payload.tags,
        is_approved=False,
    )
    session.add(document)
    await session.flush()

    for index, chunk in enumerate(rag.chunk_text(payload.content)):
        session.add(
            KnowledgeChunk(
                document_id=document.id,
                chunk_index=index,
                content=chunk,
                token_count=len(chunk.split()),
            )
        )

    await session.flush()
    return document


@router.post("/knowledge/{document_id}/approve", response_model=KnowledgeDocumentRead)
async def approve_document(
    document_id: uuid.UUID, user: StaffDep, session: DbSession
) -> KnowledgeDocument:
    """Approve a document, making its chunks retrievable by the navigator."""
    document = await session.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    document.is_approved = True
    document.approved_by_id = uuid.UUID(user.id)
    document.approved_at = datetime.now(UTC)
    await session.flush()
    return document


@router.get("/knowledge", response_model=list[KnowledgeDocumentRead])
async def list_knowledge(
    user: ContentDep, session: DbSession, approved_only: bool = False
) -> list[KnowledgeDocument]:
    stmt = select(KnowledgeDocument).order_by(KnowledgeDocument.created_at.desc())
    if approved_only:
        stmt = stmt.where(KnowledgeDocument.is_approved.is_(True))
    return list((await session.execute(stmt)).scalars())


@router.delete("/knowledge/{document_id}", response_model=Message)
async def delete_knowledge(document_id: uuid.UUID, user: StaffDep, session: DbSession) -> Message:
    document = await session.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    await session.delete(document)
    return Message(detail="Document deleted")


@router.get("/risk-flags", response_model=list[RiskFlagRead])
async def list_risk_flags(
    user: StaffDep, session: DbSession, unresolved_only: bool = True
) -> list[RiskFlag]:
    """Risk signals for coordinators. Advisory only — never an automatic sanction."""
    stmt = select(RiskFlag).order_by(RiskFlag.created_at.desc())
    if unresolved_only:
        stmt = stmt.where(RiskFlag.resolved_at.is_(None))
    return list((await session.execute(stmt.limit(200))).scalars())


@router.post("/risk-flags/{flag_id}/resolve", response_model=RiskFlagRead)
async def resolve_risk_flag(
    flag_id: uuid.UUID, user: StaffDep, session: DbSession, note: str | None = None
) -> RiskFlag:
    flag = await session.get(RiskFlag, flag_id)
    if flag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flag not found")
    flag.resolved_at = datetime.now(UTC)
    flag.resolution_note = note
    await session.flush()
    return flag
