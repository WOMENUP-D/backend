"""The portfolio: what she learned, what she can prove, and what she built.

A presentation layer, and deliberately nothing more. Every certificate, skill,
evaluation and achievement on the page is a reading of a record another service
already owns. The only things stored here for their own sake are the projects
she writes herself and whether she has chosen to make her record public.

**Achievements are derived, never stored.** Each one is keyed by the row that
backs it — `course_completed:{enrollment id}`, `certificate_earned:{certificate
id}` — so the same event cannot be awarded twice, a replayed completion adds
nothing, and an achievement disappears if the record behind it is withdrawn.
There is no table of achievements to drift out of step with reality.

**Showing evidence never changes it.** The skills section reads
`services.skills.skill_profile` and groups what it returns. Nothing here writes
a status: *learned*, *assessed* and *verified* mean exactly what Step 3 and
Step 7 made them mean, and a portfolio is not a way round either.

**A project is her word, not a verification.** Saving one records
*self-reported* evidence for the skills it names — the weakest claim there is,
and the honest one for work nobody on the platform has looked at.

**Public is built, not filtered.** The public shape is assembled field by field
from what she chose to show, for an adult who consented to it. A minor's
portfolio is never public, and neither is one belonging to a woman whose age
the platform does not know — the same protective reading the health gate takes.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, time
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    PRIVACY_POLICY_VERSION,
    AchievementType,
    ConsentScope,
    EnrollmentStatus,
    EvidenceKind,
    PortfolioSection,
    SkillStatus,
    TaskStatus,
    UserStatus,
)
from app.models.consent import ConsentLog
from app.models.learning_path import LearningPath, UserLearningPath
from app.models.opportunity import Application, Opportunity, OutcomeRecord
from app.models.portfolio import PortfolioProject
from app.models.practice import PracticalTask, TaskAttempt
from app.models.profile import Profile
from app.models.program import Certificate, Enrollment, Program
from app.models.skill import Skill, SkillEvidence, UserSkill
from app.models.user import User
from app.schemas.portfolio import (
    AchievementRead,
    PortfolioCertificate,
    PortfolioOverview,
    PortfolioPerson,
    PortfolioPractice,
    PortfolioRead,
    PortfolioSettings,
    PortfolioSettingsIn,
    PortfolioSkill,
    PortfolioSkills,
    ProjectIn,
    ProjectRead,
    ProjectUpdate,
    PublicPortfolioRead,
)
from app.schemas.skill import EvidenceRead, SkillRef
from app.services import skills as skill_service
from app.services.age_gate import band_for_profile, is_minor
from app.services.audit_service import has_consent

#: Enough for a real body of work; a ceiling on a form anybody can post to.
MAX_PROJECTS = 50
#: Outcome types a partner can confirm, and the achievement each one is.
OUTCOME_ACHIEVEMENT: dict[str, AchievementType] = {
    "employment": AchievementType.WORK_EXPERIENCE,
    "business_registered": AchievementType.BUSINESS_MILESTONE,
    "funding": AchievementType.BUSINESS_MILESTONE,
    "first_sale": AchievementType.BUSINESS_MILESTONE,
    "milestone": AchievementType.BUSINESS_MILESTONE,
}
PROJECT_SOURCE = "portfolio_project"


class PortfolioError(ValueError):
    """A request the portfolio refuses, with a machine-readable reason the
    browser renders in her language."""

    def __init__(self, reason: str, detail: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or []


def _same(title: str) -> dict:
    """A string she wrote once, as the i18n shape every title here uses."""
    return {"uz": title, "ru": title, "en": title}


# ---------------------------------------------------------------------------
# Achievements — a read model over records that already exist
# ---------------------------------------------------------------------------


async def achievements_for(
    session: AsyncSession, user_id: uuid.UUID, *, public_only: bool = False
) -> list[AchievementRead]:
    """Everything that actually happened, newest first.

    One query per source. Nothing is inferred from a profile field: listing
    "Python" on her profile is her word, and a word is not an achievement.
    With `public_only`, a project she has not made public does not appear —
    an achievement must never be the way a private project leaks.
    """
    out: list[AchievementRead] = []

    # Courses she finished — `services.learning` is the only thing that marks
    # one completed, so this is its answer rather than a second opinion.
    for enrollment, program in (
        await session.execute(
            select(Enrollment, Program)
            .join(Program, Program.id == Enrollment.program_id)
            .where(
                Enrollment.user_id == user_id,
                Enrollment.status == EnrollmentStatus.COMPLETED,
            )
        )
    ).all():
        out.append(
            AchievementRead(
                key=f"{AchievementType.COURSE_COMPLETED.value}:{enrollment.id}",
                type=AchievementType.COURSE_COMPLETED,
                title_i18n=program.title_i18n,
                earned_at=enrollment.completed_at,
                href=f"/talim/kurslar/{program.slug}",
            )
        )

    # Certificates the platform issued and has not withdrawn.
    for certificate, program in (
        await session.execute(
            select(Certificate, Program)
            .join(Enrollment, Enrollment.id == Certificate.enrollment_id)
            .join(Program, Program.id == Enrollment.program_id)
            .where(Certificate.user_id == user_id, Certificate.revoked_at.is_(None))
        )
    ).all():
        out.append(
            AchievementRead(
                key=f"{AchievementType.CERTIFICATE_EARNED.value}:{certificate.id}",
                type=AchievementType.CERTIFICATE_EARNED,
                title_i18n=program.title_i18n,
                earned_at=certificate.issued_at,
                detail=certificate.serial_number,
            )
        )

    # Routes she finished. `completed_at` is stamped once, by the path layer,
    # when the last required course completes — never by a button.
    for record, path in (
        await session.execute(
            select(UserLearningPath, LearningPath)
            .join(LearningPath, LearningPath.id == UserLearningPath.path_id)
            .where(
                UserLearningPath.user_id == user_id,
                UserLearningPath.completed_at.is_not(None),
            )
        )
    ).all():
        out.append(
            AchievementRead(
                key=f"{AchievementType.LEARNING_PATH_COMPLETED.value}:{record.id}",
                type=AchievementType.LEARNING_PATH_COMPLETED,
                title_i18n=path.title_i18n,
                earned_at=record.completed_at,
                href=f"/talim/yollar/{path.slug}" if path.is_published else None,
            )
        )

    # Practical work somebody assessed as passing. A submission waiting for a
    # verdict is not a pass, and neither is a needs-improvement.
    for attempt, task in (
        await session.execute(
            select(TaskAttempt, PracticalTask)
            .join(PracticalTask, PracticalTask.id == TaskAttempt.task_id)
            .where(
                TaskAttempt.user_id == user_id,
                TaskAttempt.status == TaskStatus.PASSED,
                TaskAttempt.passed.is_(True),
            )
        )
    ).all():
        out.append(
            AchievementRead(
                key=f"{AchievementType.PRACTICAL_TASK_PASSED.value}:{attempt.id}",
                type=AchievementType.PRACTICAL_TASK_PASSED,
                title_i18n=task.title_i18n,
                earned_at=attempt.evaluated_at,
                detail=attempt.evaluator_kind.value if attempt.evaluator_kind else None,
                href=f"/talim/amaliyot/{task.slug}",
            )
        )

    # Skills somebody outside the platform confirmed. Dated by the first
    # evidence that verified it — the moment it became true, not the moment
    # this page was drawn.
    out.extend(await _verified_skills(session, user_id))

    # Work she recorded herself. Achievements only once finished, and marked
    # as hers — nobody on the platform has checked it.
    stmt = select(PortfolioProject).where(
        PortfolioProject.user_id == user_id, PortfolioProject.completed_on.is_not(None)
    )
    if public_only:
        stmt = stmt.where(PortfolioProject.is_public.is_(True))
    for project in (await session.execute(stmt)).scalars():
        out.append(
            AchievementRead(
                key=f"{AchievementType.PROJECT_COMPLETED.value}:{project.id}",
                type=AchievementType.PROJECT_COMPLETED,
                title_i18n=_same(project.title),
                earned_at=datetime.combine(project.completed_on, time(), tzinfo=UTC),
                self_declared=True,
            )
        )

    # Results a partner platform confirmed: hired, funded, a first sale. Only
    # verified ones — a claim the partner has not confirmed is not a record.
    # `details` is the partner's own payload and is never read here.
    for outcome, opportunity in (
        await session.execute(
            select(OutcomeRecord, Opportunity)
            .outerjoin(Application, Application.id == OutcomeRecord.application_id)
            .outerjoin(Opportunity, Opportunity.id == Application.opportunity_id)
            .where(OutcomeRecord.user_id == user_id, OutcomeRecord.verified.is_(True))
        )
    ).all():
        kind = OUTCOME_ACHIEVEMENT.get(outcome.outcome_type)
        if kind is None:
            continue
        out.append(
            AchievementRead(
                key=f"{kind.value}:{outcome.id}",
                type=kind,
                title_i18n=opportunity.title_i18n if opportunity else {},
                earned_at=outcome.occurred_at,
                detail=outcome.outcome_type,
            )
        )

    # Newest first; an undated record goes last rather than being given a date.
    out.sort(
        key=lambda row: (
            row.earned_at is None,
            -(row.earned_at.timestamp()) if row.earned_at else 0,
        )
    )
    return out


async def _verified_skills(session: AsyncSession, user_id: uuid.UUID) -> list[AchievementRead]:
    verifying = [
        kind
        for kind, supports in skill_service.EVIDENCE_STATUS.items()
        if supports == SkillStatus.VERIFIED
    ]
    rows = await session.execute(
        select(UserSkill, SkillEvidence)
        .join(SkillEvidence, SkillEvidence.user_skill_id == UserSkill.id)
        .where(
            UserSkill.user_id == user_id,
            UserSkill.status == SkillStatus.VERIFIED,
            SkillEvidence.kind.in_(verifying),
            SkillEvidence.revoked_at.is_(None),
        )
        .order_by(SkillEvidence.occurred_at.asc().nullslast())
    )
    first: dict[uuid.UUID, tuple[UserSkill, SkillEvidence]] = {}
    for user_skill, evidence in rows.all():
        first.setdefault(user_skill.id, (user_skill, evidence))

    return [
        AchievementRead(
            key=f"{AchievementType.SKILL_VERIFIED.value}:{user_skill.id}",
            type=AchievementType.SKILL_VERIFIED,
            title_i18n=user_skill.skill.name_i18n,
            earned_at=evidence.occurred_at,
            detail=evidence.kind.value,
        )
        for user_skill, evidence in first.values()
    ]


# ---------------------------------------------------------------------------
# Reading the rest of the record
# ---------------------------------------------------------------------------


async def _skills(session: AsyncSession, user_id: uuid.UUID) -> PortfolioSkills:
    """Her skills, grouped by what backs them, straight from `services.skills`.

    Self-reported skills are left out: a portfolio is what she can show, and
    her own word about herself is already on her profile.
    """
    grouped = PortfolioSkills()
    for record in await skill_service.skill_profile(session, user_id):
        if record.status is None or record.status == SkillStatus.SELF_REPORTED:
            continue
        entry = PortfolioSkill(
            skill=record.skill, status=record.status, level=record.level, evidence=record.evidence
        )
        if record.status == SkillStatus.VERIFIED:
            grouped.verified.append(entry)
        elif record.status == SkillStatus.ASSESSED:
            grouped.assessed.append(entry)
        else:
            grouped.learned.append(entry)
    return grouped


async def _certificates(session: AsyncSession, user_id: uuid.UUID) -> list[PortfolioCertificate]:
    rows = (
        await session.execute(
            select(Certificate, Program)
            .join(Enrollment, Enrollment.id == Certificate.enrollment_id)
            .join(Program, Program.id == Enrollment.program_id)
            .where(Certificate.user_id == user_id, Certificate.revoked_at.is_(None))
            .order_by(Certificate.issued_at.desc())
        )
    ).all()
    index = await skill_service.SkillIndex.load(
        session, [label for _, program in rows for label in program.skills_taught]
    )
    return [
        PortfolioCertificate(
            id=certificate.id,
            serial_number=certificate.serial_number,
            issued_at=certificate.issued_at,
            verification_code=certificate.verification_code,
            program_slug=program.slug,
            program_title_i18n=program.title_i18n,
            skills=index.refs(program.skills_taught),
        )
        for certificate, program in rows
    ]


async def _practice(
    session: AsyncSession, user_id: uuid.UUID, *, passed_only: bool = False
) -> list[PortfolioPractice]:
    """Work she handed in, one entry per task, newest attempt speaking.

    Started-but-not-submitted is left out: it is not yet work. What she wrote
    and what the evaluator said stay on the task page, where only she reads
    them — neither is part of a portfolio.
    """
    rows = (
        await session.execute(
            select(TaskAttempt, PracticalTask)
            .join(PracticalTask, PracticalTask.id == TaskAttempt.task_id)
            .where(TaskAttempt.user_id == user_id, TaskAttempt.status != TaskStatus.STARTED)
            .order_by(TaskAttempt.attempt_no.desc())
        )
    ).all()
    index = await skill_service.SkillIndex.load(
        session, [label for _, task in rows for label in (task.skills_practised or [])]
    )

    seen: set[uuid.UUID] = set()
    out: list[PortfolioPractice] = []
    for attempt, task in rows:
        if task.id in seen:
            continue
        seen.add(task.id)
        if passed_only and attempt.status != TaskStatus.PASSED:
            continue
        out.append(
            PortfolioPractice(
                task_slug=task.slug,
                title_i18n=task.title_i18n,
                status=attempt.status,
                evaluated_at=attempt.evaluated_at,
                evaluator_kind=attempt.evaluator_kind,
                skills=index.refs(task.skills_practised or []),
            )
        )
    return out


async def _projects(
    session: AsyncSession, user_id: uuid.UUID, *, public_only: bool = False
) -> list[ProjectRead]:
    stmt = select(PortfolioProject).where(PortfolioProject.user_id == user_id)
    if public_only:
        stmt = stmt.where(PortfolioProject.is_public.is_(True))
    projects = list(
        (
            await session.execute(
                stmt.order_by(
                    PortfolioProject.completed_on.desc().nullsfirst(),
                    PortfolioProject.created_at.desc(),
                )
            )
        ).scalars()
    )
    refs = await _skill_refs(session, {slug for p in projects for slug in p.skill_slugs})
    return [project_read(project, refs) for project in projects]


async def _skill_refs(session: AsyncSession, slugs: set[str]) -> dict[str, SkillRef]:
    if not slugs:
        return {}
    rows = await session.execute(select(Skill).where(Skill.slug.in_(slugs)))
    return {skill.slug: skill_service.ref_for(skill) for skill in rows.scalars()}


def project_read(project: PortfolioProject, refs: dict[str, SkillRef]) -> ProjectRead:
    return ProjectRead(
        id=project.id,
        title=project.title,
        summary=project.summary,
        description=project.description,
        skills=[refs[slug] for slug in project.skill_slugs if slug in refs],
        project_url=project.project_url,
        repo_url=project.repo_url,
        demo_url=project.demo_url,
        completed_on=project.completed_on,
        is_public=project.is_public,
        created_at=project.created_at,
    )


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def may_publish(profile: Profile | None) -> tuple[bool, str | None]:
    """Whether her record may be public at all.

    Never for a minor, and never for a woman whose age the platform does not
    know: the portal serves girls from ten, and "we could not tell" is read the
    protective way here exactly as it is at the health gate.
    """
    if profile is None or is_minor(band_for_profile(profile)):
        return False, "minor"
    return True, None


def sections_of(profile: Profile | None) -> dict[str, bool]:
    """Which sections a public reader sees. Missing means shown, so a section
    added later is not silently hidden from everyone who published before."""
    chosen = (profile.portfolio_sections if profile else None) or {}
    return {section.value: bool(chosen.get(section.value, True)) for section in PortfolioSection}


def _settings(profile: Profile | None) -> PortfolioSettings:
    allowed, reason = may_publish(profile)
    return PortfolioSettings(
        is_public=bool(profile and profile.portfolio_public and allowed),
        slug=profile.portfolio_slug if profile else None,
        published_at=profile.portfolio_published_at if profile else None,
        sections=sections_of(profile),
        can_publish=allowed,
        publish_blocked=reason,
    )


async def _profile(session: AsyncSession, user_id: uuid.UUID) -> Profile | None:
    return await session.scalar(select(Profile).where(Profile.user_id == user_id))


def _person(profile: Profile | None) -> PortfolioPerson:
    """First name only. A surname is not needed to read a portfolio, and a
    public page is the last place to add one."""
    first = profile.full_name.split()[0] if profile and profile.full_name else None
    return PortfolioPerson(
        first_name=first,
        profession=profile.profession if profile else None,
        bio=profile.bio if profile else None,
    )


async def update_settings(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    payload: PortfolioSettingsIn,
    now: datetime | None = None,
) -> PortfolioSettings:
    """Change who can see her record. Every change of visibility is consented to.

    Publishing appends an accepted `public_portfolio` consent and unpublishing
    appends a withdrawn one — never an update, the same append-only record every
    other data-sharing decision on the platform leaves.
    """
    now = now or datetime.now(UTC)
    profile = await _profile(session, user_id)
    if profile is None:
        profile = Profile(user_id=user_id)
        session.add(profile)
        await session.flush()

    if payload.sections is not None:
        allowed = {section.value for section in PortfolioSection}
        unknown = sorted(set(payload.sections) - allowed)
        if unknown:
            raise PortfolioError("unknown_section", unknown)
        profile.portfolio_sections = {**sections_of(profile), **payload.sections}

    if payload.is_public is not None and payload.is_public != profile.portfolio_public:
        if payload.is_public:
            allowed_to, reason = may_publish(profile)
            if not allowed_to:
                raise PortfolioError(reason or "not_allowed")
            if not profile.portfolio_slug:
                # 96 bits of randomness. Never derived from her id, so a public
                # link cannot be used to enumerate or recognise accounts.
                profile.portfolio_slug = secrets.token_urlsafe(12)
            if profile.portfolio_published_at is None:
                profile.portfolio_published_at = now
        profile.portfolio_public = payload.is_public
        session.add(
            ConsentLog(
                user_id=user_id,
                scope=ConsentScope.PUBLIC_PORTFOLIO,
                accepted=payload.is_public,
                policy_version=PRIVACY_POLICY_VERSION,
                accepted_at=now,
                # Explicit so that two changes inside one transaction still
                # order correctly — Postgres `now()` is the transaction start.
                created_at=now,
            )
        )

    await session.flush()
    return _settings(profile)


# ---------------------------------------------------------------------------
# The portfolio, hers and public
# ---------------------------------------------------------------------------


async def portfolio_for(session: AsyncSession, user_id: uuid.UUID) -> PortfolioRead:
    """Her whole record. Every count is a count of real rows."""
    profile = await _profile(session, user_id)
    achievements = await achievements_for(session, user_id)
    certificates = await _certificates(session, user_id)
    skills = await _skills(session, user_id)
    practice = await _practice(session, user_id)
    projects = await _projects(session, user_id)

    return PortfolioRead(
        person=_person(profile),
        settings=_settings(profile),
        overview=PortfolioOverview(
            achievements=len(achievements),
            certificates=len(certificates),
            learned=len(skills.learned),
            assessed=len(skills.assessed),
            verified=len(skills.verified),
            practice_passed=sum(1 for row in practice if row.status == TaskStatus.PASSED),
            practice_submitted=sum(1 for row in practice if row.status == TaskStatus.SUBMITTED),
            projects=len(projects),
        ),
        skills=skills,
        certificates=certificates,
        achievements=achievements,
        practice=practice,
        projects=projects,
    )


def _public_skills(skills: PortfolioSkills) -> PortfolioSkills:
    """Skills with how they are backed, and nothing that names a source.

    The evidence *kind* is useful to an employer — "a mentor confirmed this".
    Its title is not public: it can be a private project's name or a practical
    task she never chose to show.
    """

    def strip(entries: list[PortfolioSkill]) -> list[PortfolioSkill]:
        return [
            PortfolioSkill(
                skill=entry.skill,
                status=entry.status,
                level=entry.level,
                evidence=[
                    EvidenceRead(kind=item.kind, supports=item.supports, level=item.level)
                    for item in entry.evidence
                    if item.kind != EvidenceKind.SELF_REPORTED
                ],
            )
            for entry in entries
        ]

    return PortfolioSkills(
        verified=strip(skills.verified),
        assessed=strip(skills.assessed),
        learned=strip(skills.learned),
    )


async def public_portfolio(session: AsyncSession, slug: str) -> PublicPortfolioRead | None:
    """What anyone with the link may read, or nothing at all.

    Every refusal is the same `None` — private, unknown, withdrawn, a minor, a
    closed account — so the endpoint cannot be used to learn which slugs
    belong to somebody.

    Two independent switches must agree: the profile flag and her latest
    consent. If they ever disagree the page is private. Failing closed is the
    only acceptable way for a publication check to fail.
    """
    profile = await session.scalar(select(Profile).where(Profile.portfolio_slug == slug))
    if profile is None or not profile.portfolio_public:
        return None
    if not may_publish(profile)[0]:
        return None
    if not await has_consent(session, profile.user_id, ConsentScope.PUBLIC_PORTFOLIO):
        return None
    account = await session.get(User, profile.user_id)
    if account is None or account.status in (UserStatus.SUSPENDED, UserStatus.DELETED):
        return None

    user_id = profile.user_id
    shown = sections_of(profile)
    person = _person(profile)

    return PublicPortfolioRead(
        first_name=person.first_name,
        profession=person.profession,
        bio=person.bio,
        skills=(
            _public_skills(await _skills(session, user_id))
            if shown[PortfolioSection.SKILLS.value]
            else None
        ),
        certificates=(
            await _certificates(session, user_id)
            if shown[PortfolioSection.CERTIFICATES.value]
            else None
        ),
        achievements=(
            await achievements_for(session, user_id, public_only=True)
            if shown[PortfolioSection.ACHIEVEMENTS.value]
            else None
        ),
        practice=(
            await _practice(session, user_id, passed_only=True)
            if shown[PortfolioSection.PRACTICE.value]
            else None
        ),
        projects=await _projects(session, user_id, public_only=True),
    )


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def _url(value: str | None, field: str) -> str | None:
    """A link a browser will open safely, or nothing.

    Only http and https: anything else is a way of putting a `javascript:` URL
    in front of whoever reads her portfolio.
    """
    text = (value or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PortfolioError("bad_url", [field])
    return text


async def resolve_skills(session: AsyncSession, labels: Iterable[str]) -> list[str]:
    """Canonical slugs for what she typed or picked — or a refusal naming the
    ones that are not skills.

    Resolved through `SkillIndex`, so "Excel", "excel" and "эксель" are one
    skill wherever the taxonomy says so. Nothing is minted: a project form is
    the one place anybody could otherwise add words to the vocabulary.
    """
    written = [label.strip() for label in labels if label and label.strip()]
    index = await skill_service.SkillIndex.load(session, written)
    slugs: list[str] = []
    unknown: list[str] = []
    for label in written:
        skill = index.get(label)
        if skill is None:
            unknown.append(label)
        elif skill.slug not in slugs:
            slugs.append(skill.slug)
    if unknown:
        raise PortfolioError("unknown_skill", unknown)
    return slugs


async def _sync_evidence(
    session: AsyncSession, project: PortfolioProject, *, remove: bool = False
) -> None:
    """Keep her skill record in step with what a project claims.

    A project is *self-reported* evidence — her word, and nothing stronger —
    so it can never raise a skill above what she says about it. Removing a
    skill from a project, or the project itself, withdraws that one claim and
    nothing else: a course she finished stays on the record.
    """
    wanted = set() if remove else set(project.skill_slugs)
    now = datetime.now(UTC)

    if wanted:
        rows = await session.execute(select(Skill).where(Skill.slug.in_(wanted)))
        for skill in rows.scalars():
            await skill_service.record_evidence(
                session,
                user_id=project.user_id,
                skill=skill,
                kind=EvidenceKind.SELF_REPORTED,
                source_type=PROJECT_SOURCE,
                source_id=str(project.id),
                occurred_at=now,
            )

    stale = await session.execute(
        select(SkillEvidence, UserSkill, Skill)
        .join(UserSkill, UserSkill.id == SkillEvidence.user_skill_id)
        .join(Skill, Skill.id == UserSkill.skill_id)
        .where(
            UserSkill.user_id == project.user_id,
            SkillEvidence.source_type == PROJECT_SOURCE,
            SkillEvidence.source_id == str(project.id),
            SkillEvidence.revoked_at.is_(None),
        )
    )
    for evidence, user_skill, skill in stale.all():
        if skill.slug in wanted:
            continue
        evidence.revoked_at = now
        await session.flush()
        await skill_service.recompute(session, user_skill)


async def own_project(
    session: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> PortfolioProject | None:
    """Her project, or nothing. Never somebody else's, whatever id is sent."""
    return await session.scalar(
        select(PortfolioProject).where(
            PortfolioProject.id == project_id, PortfolioProject.user_id == user_id
        )
    )


async def create_project(
    session: AsyncSession, *, user_id: uuid.UUID, payload: ProjectIn
) -> tuple[PortfolioProject, bool]:
    """Save a project. Returns `(project, created)`.

    A retry carrying the same `client_ref` returns the project the first try
    made, so a double tap or a dropped connection cannot leave two copies.
    """
    if payload.client_ref is not None:
        existing = await session.scalar(
            select(PortfolioProject).where(
                PortfolioProject.user_id == user_id,
                PortfolioProject.client_ref == payload.client_ref,
            )
        )
        if existing is not None:
            return existing, False

    count = len(
        list(
            (
                await session.execute(
                    select(PortfolioProject.id).where(PortfolioProject.user_id == user_id)
                )
            ).scalars()
        )
    )
    if count >= MAX_PROJECTS:
        raise PortfolioError("too_many")

    project = PortfolioProject(
        user_id=user_id,
        title=payload.title.strip(),
        summary=(payload.summary or "").strip() or None,
        description=(payload.description or "").strip() or None,
        skill_slugs=await resolve_skills(session, payload.skills),
        project_url=_url(payload.project_url, "project_url"),
        repo_url=_url(payload.repo_url, "repo_url"),
        demo_url=_url(payload.demo_url, "demo_url"),
        completed_on=payload.completed_on,
        is_public=payload.is_public,
        client_ref=payload.client_ref,
    )
    session.add(project)
    await session.flush()
    await _sync_evidence(session, project)
    return project, True


async def update_project(
    session: AsyncSession, *, project: PortfolioProject, payload: ProjectUpdate
) -> PortfolioProject:
    changes = payload.model_dump(exclude_unset=True)
    if "title" in changes and changes["title"] is not None:
        project.title = changes["title"].strip()
    for field in ("summary", "description"):
        if field in changes:
            setattr(project, field, (changes[field] or "").strip() or None)
    for field in ("project_url", "repo_url", "demo_url"):
        if field in changes:
            setattr(project, field, _url(changes[field], field))
    if "completed_on" in changes:
        project.completed_on = changes["completed_on"]
    if "is_public" in changes and changes["is_public"] is not None:
        project.is_public = changes["is_public"]
    if "skills" in changes and changes["skills"] is not None:
        project.skill_slugs = await resolve_skills(session, changes["skills"])
    await session.flush()
    await _sync_evidence(session, project)
    return project


async def delete_project(session: AsyncSession, *, project: PortfolioProject) -> None:
    """Remove a project and withdraw the claims it made.

    The evidence rows are marked withdrawn, not deleted — the same rule every
    other evidence source follows — and the skills they touched are
    recomputed from what is left.
    """
    await _sync_evidence(session, project, remove=True)
    await session.delete(project)
    await session.flush()


async def project_refs(session: AsyncSession, project: PortfolioProject) -> dict[str, SkillRef]:
    return await _skill_refs(session, set(project.skill_slugs))
