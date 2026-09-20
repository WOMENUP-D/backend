"""Opportunity discovery: the catalogue, one listing, applying, and tracking.

Built on what already exists rather than beside it. Listings are the partner
feed in `opportunities`; applications are `applications`; consent is the
append-only `consent_logs`; what a partner receives is `minimal_profile_payload`
and it leaves only through `integration_gateway.queue_event`. This module adds
the reading a woman needs to act on them — and nothing it says is invented.

**Matching** is the recommendation engine's own: her held skills and the skill
index from its `LearnerContext`, and its `overlap`. What this module adds is
the *explanation*, and every line of it is a record: a skill the listing asks
for and how she holds it (learned, assessed, verified, or her own word), the
course that taught it, her chosen career direction, her region. A skill she is
missing comes with the courses on WomanUP that teach it — or with none, said
plainly. Nothing here says she is eligible for work, only what the platform
can check.

**Eligibility** is `services.eligibility`, the one rule the engine, the career
page and the apply endpoint also read.

**Applying** takes her confirmation on the page. For a listing that came from a
partner platform, the confirm step shows exactly which fields that platform
receives; ticking the box records her consent for that platform as a new row
in `consent_logs` (append-only, as every consent is) before the application is
queued. Without it — and with no consent already on file — nothing is created
and nothing leaves. A listing from WomanUP itself sends nothing anywhere.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    BUSINESS_OPPORTUNITY_TYPES,
    PRIVACY_POLICY_VERSION,
    ApplicationStatus,
    CareerCategory,
    ConsentScope,
    DataClassification,
    DimensionBand,
    EnrollmentStatus,
    IntegrationSystem,
    Language,
    OpportunitySource,
    OpportunityType,
    ScoreDimension,
    SkillStatus,
)
from app.models.career_path import CareerPath
from app.models.opportunity import Application, Opportunity, SavedOpportunity
from app.models.organization import Organization
from app.models.profile import Profile
from app.models.user import User
from app.schemas.opportunity import (
    ApplicationRead,
    BusinessRead,
    EligibilityRead,
    MatchReason,
    MissingSkill,
    OpportunityCard,
    OpportunityDetail,
    OpportunityDiscover,
    OpportunityFacets,
    OpportunityFit,
    OpportunityRead,
    OpportunitySummary,
    OrganizationBrief,
    SharingRead,
    SkillFacet,
)
from app.services import career_path as career_service
from app.services import eligibility
from app.services import employer as employer_service
from app.services import recommendation as engine
from app.services import skills as skill_service
from app.services.age_gate import age_from_profile
from app.services.audit_service import has_consent, record_audit, record_consent
from app.services.integration_gateway import (
    CONSENT_FOR_SYSTEM,
    minimal_profile_payload,
    queue_event,
)
from app.services.score_insights import band_for

#: The partner platform a listing's application goes to.
SOURCE_TO_SYSTEM = {
    OpportunitySource.EDU_JOB: IntegrationSystem.EDU_JOB,
    OpportunitySource.INVEST_HUB: IntegrationSystem.INVEST_HUB,
    OpportunitySource.COMMERCE: IntegrationSystem.COMMERCE,
}

#: Exactly the fields a partner receives, read off the function that builds
#: the payload rather than written out a second time — so the confirm step
#: cannot promise less than what is sent.
SHARED_FIELDS: tuple[str, ...] = tuple(
    minimal_profile_payload(
        SimpleNamespace(id=uuid.UUID(int=0), region=None, language=Language.UZ), None
    )
)

#: A woman may withdraw an application that is still open.
WITHDRAWABLE = (ApplicationStatus.DRAFT, ApplicationStatus.SUBMITTED, ApplicationStatus.IN_REVIEW)

#: The strongest evidence first, when choosing which source to name.
_SUPPORT_RANK = {
    SkillStatus.VERIFIED: 0,
    SkillStatus.ASSESSED: 1,
    SkillStatus.LEARNED: 2,
    SkillStatus.SELF_REPORTED: 3,
}
_OPEN_ENROLLMENT = (EnrollmentStatus.ENROLLED, EnrollmentStatus.IN_PROGRESS)
_NO_DEADLINE = datetime.max.replace(tzinfo=UTC)
CATALOGUE_LIMIT = 500
MAX_SKILL_FACETS = 12
PROGRAMS_PER_GAP = 2


class ApplyError(Exception):
    """Applying was refused. `reason` is a key the page turns into a sentence."""

    def __init__(self, reason: str, status_code: int, detail: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.detail = detail or {}


@dataclass(slots=True)
class Filters:
    type: OpportunityType | None = None
    region: str | None = None
    skill: str | None = None
    search: str | None = None
    include_closed: bool = False
    #: Only listings that support a business of her own.
    business: bool = False
    #: `match` (her best fit first) or `deadline` (soonest first).
    sort: str | None = None
    page: int = 1
    size: int = 20


# ---------------------------------------------------------------------------
# Her side: read once per request, only for her
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Reader:
    ctx: engine.LearnerContext
    her: career_service.HerRecords
    #: skill slug -> the title of the strongest evidence that backs it.
    sources: dict[str, dict] = field(default_factory=dict)
    career: CareerPath | None = None
    career_keys: set[str] = field(default_factory=set)
    applications: dict[uuid.UUID, Application] = field(default_factory=dict)
    saved: set[uuid.UUID] = field(default_factory=set)


async def reader_for(session: AsyncSession, user_id: uuid.UUID) -> Reader:
    """Everything about her that explaining a listing needs, in one place."""
    ctx = await engine.load_context(session, user_id)
    her = await career_service.her_records(session, user_id)

    sources: dict[str, dict] = {}
    for item in await skill_service.skill_profile(session, user_id):
        if not item.skill.slug:
            continue
        backed = sorted(
            (ev for ev in item.evidence if ev.title_i18n),
            key=lambda ev: _SUPPORT_RANK.get(ev.supports, 9),
        )
        if backed:
            sources[item.skill.slug] = backed[0].title_i18n

    choice = await career_service.choice_of(session, user_id)
    career = await session.get(CareerPath, choice.career_path_id) if choice else None
    if career is not None:
        await _widen(session, ctx.index, career.skill_slugs)

    applications = {
        row.opportunity_id: row
        for row in (
            await session.execute(select(Application).where(Application.user_id == user_id))
        ).scalars()
    }
    saved = set(
        (
            await session.scalars(
                select(SavedOpportunity.opportunity_id).where(SavedOpportunity.user_id == user_id)
            )
        ).all()
    )
    return Reader(
        ctx=ctx,
        her=her,
        sources=sources,
        career=career,
        career_keys=career_service.keys_of(ctx, career.skill_slugs) if career else set(),
        applications=applications,
        saved=saved,
    )


async def _widen(
    session: AsyncSession, index: skill_service.SkillIndex, labels: Iterable[str]
) -> None:
    """Make sure the index knows every label on the listings being read."""
    extra = await skill_service.SkillIndex.load(session, labels)
    for alias, skill in extra.by_alias.items():
        index.by_alias.setdefault(alias, skill)


# ---------------------------------------------------------------------------
# One listing, read against her
# ---------------------------------------------------------------------------


def _eligibility(opportunity: Opportunity, reader: Reader | None) -> EligibilityRead | None:
    if reader is None:
        return None
    verdict = eligibility.check(opportunity, age=reader.ctx.age)
    return EligibilityRead(
        status=verdict.status,
        reason=verdict.reason,
        age_min=verdict.age_min,
        age_max=verdict.age_max,
        may_apply=verdict.may_apply,
    )


def _fit(
    reader: Reader,
    index: skill_service.SkillIndex,
    opportunity: Opportunity,
    *,
    with_programs: bool,
) -> OpportunityFit:
    ctx = reader.ctx
    labels = opportunity.required_skills or []
    keys = {index.key(label) for label in labels if label and label.strip()}
    on_career = (
        reader.career is not None
        and career_service.leads_to(reader.career, opportunity)
        and bool(keys & reader.career_keys)
    )

    matched, missing, _ = engine.overlap(index, ctx.held, labels) if labels else ([], [], 0)
    reasons: list[MatchReason] = []
    for ref in matched:
        key = index.key(ref.slug or ref.label)
        reasons.append(
            MatchReason(
                kind="skill",
                skill=ref,
                status=career_service.status_of(ctx, reader.her, key),
                source_i18n=reader.sources.get(key, {}),
            )
        )
    if on_career and reader.career is not None:
        reasons.append(
            MatchReason(
                kind="career",
                career_slug=reader.career.slug,
                career_title_i18n=reader.career.title_i18n,
            )
        )
    if ctx.region and opportunity.region == ctx.region:
        reasons.append(MatchReason(kind="region"))

    gaps: list[MissingSkill] = []
    for ref in missing:
        key = index.key(ref.slug or ref.label)
        programs = []
        if with_programs:
            for program in ctx.catalogue:
                enrollment = ctx.enrollments.get(program.id)
                if enrollment is not None and enrollment.status == EnrollmentStatus.COMPLETED:
                    continue
                if key in {index.key(label) for label in program.skills_taught}:
                    programs.append(
                        {
                            "id": str(program.id),
                            "slug": program.slug,
                            "title_i18n": program.title_i18n,
                            "in_progress": bool(
                                enrollment is not None and enrollment.status in _OPEN_ENROLLMENT
                            ),
                        }
                    )
                if len(programs) >= PROGRAMS_PER_GAP:
                    break
        gaps.append(MissingSkill(skill=ref, programs=programs))

    return OpportunityFit(
        have=len(matched),
        total=len(matched) + len(missing),
        reasons=reasons,
        missing=gaps,
        on_career=on_career,
    )


def brief(org: Organization | None) -> OrganizationBrief | None:
    """An organisation as a listing names it: public fields only, and a link to
    its page only when its owner published one."""
    if org is None:
        return None
    return OrganizationBrief(
        id=org.id,
        name=org.name,
        kind=org.kind.value,
        is_verified=org.is_verified,
        slug=org.slug if org.is_public else None,
        logo_url=org.logo_url if org.is_public else None,
    )


async def _orgs(
    session: AsyncSession, rows: Iterable[Opportunity]
) -> dict[uuid.UUID, Organization]:
    ids = {row.organization_id for row in rows if row.organization_id}
    if not ids:
        return {}
    found = await session.execute(select(Organization).where(Organization.id.in_(ids)))
    return {org.id: org for org in found.scalars()}


def _published_by_active(row: Opportunity, orgs: dict[uuid.UUID, Organization]) -> bool:
    """A listing an organisation published is shown only while it is active."""
    if row.organization_id is None:
        return True
    org = orgs.get(row.organization_id)
    return org is not None and org.is_active


def _card(
    opportunity: Opportunity,
    index: skill_service.SkillIndex,
    reader: Reader | None,
    *,
    now: datetime,
    with_programs: bool = False,
    org: Organization | None = None,
) -> OpportunityCard:
    application = reader.applications.get(opportunity.id) if reader else None
    return OpportunityCard(
        **OpportunityRead.model_validate(opportunity).model_dump(),
        organization=brief(org),
        is_open=eligibility.is_open(opportunity, now),
        skills=index.refs(opportunity.required_skills or []),
        fit=_fit(reader, index, opportunity, with_programs=with_programs) if reader else None,
        eligibility=_eligibility(opportunity, reader),
        application_status=application.status if application else None,
        saved=bool(reader and opportunity.id in reader.saved),
    )


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def _search(stmt, text: str):
    pattern = f"%{text.lower()}%"
    return stmt.where(
        or_(
            *(
                func.lower(Opportunity.title_i18n.op("->>")(language)).like(pattern)
                for language in ("uz", "ru", "en")
            ),
            func.lower(Opportunity.organisation).like(pattern),
        )
    )


async def discover(
    session: AsyncSession, user_id: uuid.UUID | None, filters: Filters
) -> OpportunityDiscover:
    """Listings, filtered the way a woman looks for one, with her fit on each.

    Filters are applied in memory over the open catalogue — it is hundreds of
    rows, not millions — so each facet can be counted with every *other*
    filter applied, and no option is offered that would return nothing.
    """
    now = datetime.now(UTC)
    # Events have a catalogue of their own, read by date (`services.events`).
    stmt = select(Opportunity).where(
        Opportunity.is_active.is_(True), Opportunity.starts_at.is_(None)
    )
    if not filters.include_closed:
        stmt = stmt.where(or_(Opportunity.deadline.is_(None), Opportunity.deadline > now))
    if filters.search and filters.search.strip():
        stmt = _search(stmt, filters.search.strip()[:100])
    if filters.business:
        stmt = stmt.where(Opportunity.type.in_(list(BUSINESS_OPPORTUNITY_TYPES)))
    rows = list((await session.execute(stmt.limit(CATALOGUE_LIMIT))).scalars())
    orgs = await _orgs(session, rows)
    rows = [row for row in rows if _published_by_active(row, orgs)]

    reader = await reader_for(session, user_id) if user_id is not None else None
    labels = [label for row in rows for label in (row.required_skills or [])]
    if filters.skill:
        labels.append(filters.skill)
    if reader is not None:
        index = reader.ctx.index
        await _widen(session, index, labels)
    else:
        index = await skill_service.SkillIndex.load(session, labels)

    keys = {row.id: {index.key(label) for label in (row.required_skills or [])} for row in rows}
    wanted_skill = index.key(filters.skill) if filters.skill else None

    def passes(row: Opportunity, *, skip: str | None = None) -> bool:
        if skip != "type" and filters.type and row.type != filters.type:
            return False
        if skip != "region" and filters.region and row.region != filters.region:
            return False
        return not (skip != "skill" and wanted_skill and wanted_skill not in keys[row.id])

    facets = OpportunityFacets(
        types=dict(
            Counter(getattr(r.type, "value", r.type) for r in rows if passes(r, skip="type"))
        ),
        regions=dict(Counter(r.region for r in rows if r.region and passes(r, skip="region"))),
        skills=[
            SkillFacet(skill=index.ref(key if not key.startswith("label:") else key[6:]), count=n)
            for key, n in Counter(
                key for r in rows if passes(r, skip="skill") for key in keys[r.id]
            ).most_common(MAX_SKILL_FACETS)
        ],
    )

    chosen = [row for row in rows if passes(row)]
    cards = [
        _card(row, index, reader, now=now, org=orgs.get(row.organization_id)) for row in chosen
    ]

    sort = filters.sort or ("match" if reader else "deadline")

    def by_deadline(card: OpportunityCard):
        return (not card.is_open, card.deadline or _NO_DEADLINE)

    if sort == "match" and reader is not None:
        cards.sort(
            key=lambda card: (
                not card.is_open,
                0 if card.eligibility and card.eligibility.may_apply else 1,
                -(card.fit.have / card.fit.total if card.fit and card.fit.total else 0),
                0 if card.fit and card.fit.on_career else 1,
                card.deadline or _NO_DEADLINE,
            )
        )
    else:
        cards.sort(key=by_deadline)

    size = max(1, min(filters.size, 50))
    page = max(1, filters.page)
    start = (page - 1) * size
    return OpportunityDiscover(
        items=cards[start : start + size],
        total=len(cards),
        page=page,
        size=size,
        facets=facets,
        signed_in=reader is not None,
        applications=sum(
            1 for app in (reader.applications.values() if reader else []) if app.submitted_at
        ),
        saved=len(reader.saved) if reader else 0,
    )


# ---------------------------------------------------------------------------
# One listing
# ---------------------------------------------------------------------------


async def visible(session: AsyncSession, opportunity_id: uuid.UUID) -> Opportunity | None:
    """A listing anyone may open: one the partner has not taken down. A closed
    one still opens — she may have applied to it and want to read it again."""
    opportunity = await session.get(Opportunity, opportunity_id)
    if opportunity is None or not opportunity.is_active:
        return None
    if opportunity.organization_id is not None:
        org = await session.get(Organization, opportunity.organization_id)
        if org is None or not org.is_active:
            return None
    return opportunity


async def _sharing(
    session: AsyncSession, opportunity: Opportunity, user_id: uuid.UUID | None
) -> SharingRead:
    if opportunity.source == OpportunitySource.ORGANIZATION and opportunity.organization_id:
        # An organisation on WomanUP: her profile is read by that organisation
        # here, under a consent that names it — nothing leaves the platform.
        org = await session.get(Organization, opportunity.organization_id)
        return SharingRead(
            partner=OpportunitySource.ORGANIZATION,
            consent_scope=ConsentScope.SHARE_WITH_EMPLOYER.value,
            consent_given=bool(
                user_id and org and await employer_service.shares_with(session, user_id, org)
            ),
            fields=list(employer_service.EMPLOYER_FIELDS),
            organization=brief(org),
        )
    system = SOURCE_TO_SYSTEM.get(opportunity.source)
    if system is None:
        # A WomanUP listing: the application stays on the platform.
        return SharingRead()
    scope = CONSENT_FOR_SYSTEM[system]
    return SharingRead(
        partner=opportunity.source,
        consent_scope=scope.value,
        consent_given=bool(user_id and await has_consent(session, user_id, scope)),
        fields=list(SHARED_FIELDS),
    )


async def detail(
    session: AsyncSession, opportunity: Opportunity, user_id: uuid.UUID | None
) -> OpportunityDetail:
    now = datetime.now(UTC)
    reader = await reader_for(session, user_id) if user_id is not None else None
    labels = list(opportunity.required_skills or [])
    if reader is not None:
        index = reader.ctx.index
        await _widen(session, index, labels)
    else:
        index = await skill_service.SkillIndex.load(session, labels)

    org = (
        await session.get(Organization, opportunity.organization_id)
        if opportunity.organization_id
        else None
    )
    card = _card(opportunity, index, reader, now=now, with_programs=True, org=org)
    application = reader.applications.get(opportunity.id) if reader else None
    return OpportunityDetail(
        **card.model_dump(),
        sharing=await _sharing(session, opportunity, user_id),
        application=application_read(application, opportunity, now=now) if application else None,
        other_requirements=eligibility.unreadable(opportunity),
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


async def apply(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    opportunity: Opportunity,
    consent: bool,
    ip_address: str | None = None,
    user_agent: str | None = None,
    answers: dict | None = None,
) -> Application:
    """Submit her application, after every check the server can make.

    In order: the listing is open and she may apply to it; she has not applied
    already; and — for a partner listing — consent for that partner is on file
    or given now. Only then is the application written and the partner event
    queued, through the one outbound path, which checks consent once more.
    """
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))
    verdict = eligibility.check(opportunity, age=age_from_profile(profile))
    if not verdict.may_apply:
        raise ApplyError(
            verdict.reason or "not_eligible",
            409 if verdict.reason == "closed" else 403,
            {"age_min": verdict.age_min, "age_max": verdict.age_max},
        )

    existing = await session.scalar(
        select(Application).where(
            Application.user_id == user_id, Application.opportunity_id == opportunity.id
        )
    )
    if existing is not None:
        raise ApplyError("already_applied", 409)

    if opportunity.source == OpportunitySource.ORGANIZATION and opportunity.organization_id:
        org = await session.get(Organization, opportunity.organization_id)
        if org is None or not org.is_active:
            raise ApplyError("closed", 409)
        if not await employer_service.shares_with(session, user_id, org):
            if not consent:
                raise ApplyError(
                    "consent_required", 403, {"scope": ConsentScope.SHARE_WITH_EMPLOYER.value}
                )
            # Her consent to share with this organisation — and only this one.
            await record_consent(
                session,
                user_id=user_id,
                scope=ConsentScope.SHARE_WITH_EMPLOYER,
                accepted=True,
                policy_version=PRIVACY_POLICY_VERSION,
                ip_address=ip_address,
                user_agent=user_agent,
                subject_ref=employer_service.subject(org),
            )
            await record_audit(
                session,
                action="consent.grant",
                entity_type="consent",
                entity_id=ConsentScope.SHARE_WITH_EMPLOYER.value,
                actor_id=user_id,
                classification=DataClassification.PERSONAL,
                changes={"via": "application", "organization_id": str(org.id)},
                ip_address=ip_address,
            )
            await session.flush()

    system = SOURCE_TO_SYSTEM.get(opportunity.source)
    if system is not None:
        scope = CONSENT_FOR_SYSTEM[system]
        if not await has_consent(session, user_id, scope):
            if not consent:
                raise ApplyError("consent_required", 403, {"scope": scope.value})
            # Her agreement, given on the confirm step that listed the fields.
            # A new row, like every consent — nothing earlier is rewritten.
            entry = await record_consent(
                session,
                user_id=user_id,
                scope=scope,
                accepted=True,
                policy_version=PRIVACY_POLICY_VERSION,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            # Stamped with the moment she ticked the box, not the start of the
            # transaction: "latest row wins" must order this after any earlier
            # withdrawal, even one written in the same transaction.
            entry.created_at = entry.accepted_at
            await record_audit(
                session,
                action="consent.grant",
                entity_type="consent",
                entity_id=scope.value,
                actor_id=user_id,
                classification=DataClassification.PERSONAL,
                changes={"via": "application", "opportunity_id": str(opportunity.id)},
                ip_address=ip_address,
            )
            await session.flush()

    now = datetime.now(UTC)
    application = Application(
        user_id=user_id,
        opportunity_id=opportunity.id,
        status=ApplicationStatus.SUBMITTED,
        payload=answers or {},
        submitted_at=now,
        status_history=[{"status": ApplicationStatus.SUBMITTED.value, "at": now.isoformat()}],
    )
    session.add(application)
    await session.flush()

    if system is not None:
        record = await session.get(User, user_id)
        await queue_event(
            session,
            system=system,
            event_type="application.submit",
            user_id=user_id,
            reference=str(application.id),
            payload={
                "application_id": str(application.id),
                "opportunity_external_id": opportunity.external_id,
                "applicant": minimal_profile_payload(record, profile),
                "answers": answers or {},
            },
        )
    return application


# ---------------------------------------------------------------------------
# Tracking, withdrawing, saving
# ---------------------------------------------------------------------------


def application_read(
    application: Application, opportunity: Opportunity | None, *, now: datetime | None = None
) -> ApplicationRead:
    """An application as she reads it. A partner's note is internal and is
    left out; only the status and when it changed cross to the browser."""
    now = now or datetime.now(UTC)
    return ApplicationRead(
        id=application.id,
        opportunity_id=application.opportunity_id,
        status=application.status,
        external_application_id=application.external_application_id,
        submitted_at=application.submitted_at,
        resolved_at=application.resolved_at,
        created_at=application.created_at,
        status_history=[
            {"status": entry.get("status"), "at": entry.get("at")}
            for entry in (application.status_history or [])
            if isinstance(entry, dict)
        ],
        opportunity=(
            OpportunitySummary(
                id=opportunity.id,
                type=opportunity.type,
                source=opportunity.source,
                title_i18n=opportunity.title_i18n,
                organisation=opportunity.organisation,
                region=opportunity.region,
                deadline=opportunity.deadline,
                is_open=eligibility.is_open(opportunity, now),
                starts_at=opportunity.starts_at,
            )
            if opportunity is not None
            else None
        ),
        can_withdraw=application.status in WITHDRAWABLE,
    )


async def applications_for(session: AsyncSession, user_id: uuid.UUID) -> list[ApplicationRead]:
    """Her applications, newest first, each with the listing it went to."""
    rows = (
        await session.execute(
            select(Application, Opportunity)
            .join(Opportunity, Opportunity.id == Application.opportunity_id)
            .where(Application.user_id == user_id)
            .order_by(Application.created_at.desc())
        )
    ).all()
    now = datetime.now(UTC)
    return [application_read(app, opp, now=now) for app, opp in rows]


async def save(session: AsyncSession, *, user_id: uuid.UUID, opportunity: Opportunity) -> None:
    """Keep a listing. Idempotent; tells nobody."""
    exists = await session.scalar(
        select(SavedOpportunity.id).where(
            SavedOpportunity.user_id == user_id,
            SavedOpportunity.opportunity_id == opportunity.id,
        )
    )
    if exists is None:
        session.add(SavedOpportunity(user_id=user_id, opportunity_id=opportunity.id))
        await session.flush()


async def unsave(session: AsyncSession, *, user_id: uuid.UUID, opportunity_id: uuid.UUID) -> None:
    row = await session.scalar(
        select(SavedOpportunity).where(
            SavedOpportunity.user_id == user_id,
            SavedOpportunity.opportunity_id == opportunity_id,
        )
    )
    if row is not None:
        await session.delete(row)
        await session.flush()


async def saved_for(session: AsyncSession, user_id: uuid.UUID) -> list[OpportunityCard]:
    """What she kept, newest first, read the way the catalogue reads it."""
    rows: Sequence[Opportunity] = list(
        (
            await session.execute(
                select(Opportunity)
                .join(SavedOpportunity, SavedOpportunity.opportunity_id == Opportunity.id)
                .where(SavedOpportunity.user_id == user_id, Opportunity.is_active.is_(True))
                .order_by(SavedOpportunity.created_at.desc())
            )
        ).scalars()
    )
    if not rows:
        return []
    reader = await reader_for(session, user_id)
    await _widen(
        session, reader.ctx.index, [label for row in rows for label in row.required_skills]
    )
    now = datetime.now(UTC)
    orgs = await _orgs(session, rows)
    return [
        _card(row, reader.ctx.index, reader, now=now, org=orgs.get(row.organization_id))
        for row in rows
        if _published_by_active(row, orgs)
    ]


# ---------------------------------------------------------------------------
# For your business
# ---------------------------------------------------------------------------

#: What her profile's employment status says when she runs a business of her
#: own. The onboarding answer is stored as she wrote it, so every spelling the
#: platform has seen is listed, compared through the skill layer's folding.
OWN_BUSINESS = frozenset(
    skill_service.normalise(label)
    for label in (
        "oʻz ishi bor",
        "o'z ishi bor",
        "oz ishi bor",
        "tadbirkor",
        "own business",
        "self-employed",
        "self employed",
        "entrepreneur",
        "свой бизнес",
        "собственный бизнес",
        "предприниматель",
    )
)
MAX_BUSINESS = 6


async def business_for(session: AsyncSession, user_id: uuid.UUID) -> BusinessRead:
    """A few real listings that support a business of her own, each with the
    reasons it is here — never everything, and never a listing with no reason.

    The listings are the engine's own open, eligibility-filtered set, narrowed
    to the kinds that support a business. The reasons are records: skills she
    holds that it asks for, the own-business career direction she chose, her
    profile saying she runs a business, an interest she chose that it is
    about, and — once she has a score — an entrepreneurship reading that is
    worth developing.
    """
    reader = await reader_for(session, user_id)
    ctx = reader.ctx
    index = ctx.index
    profile = await session.scalar(select(Profile).where(Profile.user_id == user_id))

    status = skill_service.normalise(profile.employment_status or "") if profile else ""
    own = bool(status) and status in OWN_BUSINESS
    interests = [label for label in (profile.interests if profile else []) if label]
    await _widen(session, index, interests)
    interest_keys = {index.key(label) for label in interests}
    career = (
        reader.career
        if reader.career and reader.career.category == CareerCategory.OWN_BUSINESS
        else None
    )
    score = ctx.scores.get(ScoreDimension.ENTREPRENEURSHIP)
    developing = score is not None and band_for(score.current) != DimensionBand.STRONG

    candidates = [item for item in ctx.opportunities if item.type in BUSINESS_OPPORTUNITY_TYPES]
    await _widen(session, index, [label for item in candidates for label in item.required_skills])
    orgs = await _orgs(session, candidates)
    now = datetime.now(UTC)

    ranked: list[tuple[tuple, OpportunityCard]] = []
    for item in candidates:
        if not _published_by_active(item, orgs):
            continue
        card = _card(item, index, reader, now=now, org=orgs.get(item.organization_id))
        fit = card.fit
        if fit is None:
            continue
        reasons = [reason for reason in fit.reasons if reason.kind in ("skill", "region")]
        if career is not None and career_service.leads_to(career, item):
            reasons.append(
                MatchReason(
                    kind="career", career_slug=career.slug, career_title_i18n=career.title_i18n
                )
            )
        if own:
            reasons.append(MatchReason(kind="own_business"))
        overlap = [label for label in item.required_skills if index.key(label) in interest_keys]
        if overlap:
            reasons.append(MatchReason(kind="interest", skill=index.ref(overlap[0])))
        if developing and score is not None:
            reasons.append(
                MatchReason(
                    kind="score",
                    dimension=ScoreDimension.ENTREPRENEURSHIP.value,
                    score=round(score.current),
                )
            )
        # A listing with nothing but her region to say for it is not "for her".
        if not [reason for reason in reasons if reason.kind != "region"]:
            continue
        fit.reasons = reasons
        coverage = fit.have / fit.total if fit.total else 0
        ranked.append(((-len(reasons), -coverage, item.deadline or _NO_DEADLINE), card))

    ranked.sort(key=lambda row: row[0])
    return BusinessRead(
        items=[card for _, card in ranked[:MAX_BUSINESS]],
        total_open=len(candidates),
        signals={
            "own_business": own,
            "career": career is not None,
            "interests": bool(interests),
            "score": score is not None,
        },
    )
