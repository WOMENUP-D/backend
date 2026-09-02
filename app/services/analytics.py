"""KPI computation for the management dashboard (section 09)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ApplicationStatus,
    EnrollmentStatus,
    OpportunitySource,
    Region,
)
from app.models.assessment import Assessment, DevelopmentScore
from app.models.audit import RiskFlag
from app.models.mentor import MentorSession
from app.models.opportunity import Application, OutcomeRecord
from app.models.plan import DevelopmentPlan
from app.models.program import Enrollment
from app.models.user import User
from app.schemas.admin import (
    DashboardFilter,
    DashboardOverview,
    KpiSnapshot,
    KpiValue,
    RegionCoverage,
)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator * 100 / denominator, 2) if denominator else 0.0


def _apply_user_filters(stmt: Select, filters: DashboardFilter) -> Select:
    if filters.region:
        stmt = stmt.where(User.region == filters.region)
    if filters.date_from:
        stmt = stmt.where(User.created_at >= filters.date_from)
    if filters.date_to:
        stmt = stmt.where(User.created_at <= filters.date_to)
    return stmt


async def kpi_snapshot(
    session: AsyncSession, filters: DashboardFilter | None = None
) -> KpiSnapshot:
    """The MVP KPI set, computed live.

    At national scale these should be served from pre-aggregated tables; the
    live queries are correct and adequate for the pilot.
    """
    filters = filters or DashboardFilter()

    registered = (
        await session.scalar(_apply_user_filters(select(func.count(User.id)), filters)) or 0
    )
    onboarded = (
        await session.scalar(
            _apply_user_filters(
                select(func.count(User.id)).where(User.onboarding_completed_at.is_not(None)),
                filters,
            )
        )
        or 0
    )
    assessed = (
        await session.scalar(
            select(func.count(func.distinct(Assessment.user_id))).where(
                Assessment.completed_at.is_not(None)
            )
        )
        or 0
    )
    plans_accepted = (
        await session.scalar(
            select(func.count(func.distinct(DevelopmentPlan.user_id))).where(
                DevelopmentPlan.accepted_at.is_not(None)
            )
        )
        or 0
    )
    plans_generated = (
        await session.scalar(select(func.count(func.distinct(DevelopmentPlan.user_id)))) or 0
    )

    enrolled = await session.scalar(select(func.count(Enrollment.id))) or 0
    completed = (
        await session.scalar(
            select(func.count(Enrollment.id)).where(Enrollment.status == EnrollmentStatus.COMPLETED)
        )
        or 0
    )

    applications = await session.scalar(select(func.count(Application.id))) or 0
    accepted_apps = (
        await session.scalar(
            select(func.count(Application.id)).where(
                Application.status == ApplicationStatus.ACCEPTED
            )
        )
        or 0
    )

    employed = (
        await session.scalar(
            select(func.count(OutcomeRecord.id)).where(OutcomeRecord.outcome_type == "employment")
        )
        or 0
    )
    businesses = (
        await session.scalar(
            select(func.count(OutcomeRecord.id)).where(
                OutcomeRecord.outcome_type == "business_registered"
            )
        )
        or 0
    )
    sellers = (
        await session.scalar(
            select(func.count(func.distinct(OutcomeRecord.user_id))).where(
                OutcomeRecord.source == OpportunitySource.COMMERCE
            )
        )
        or 0
    )

    cutoff = datetime.now(UTC) - timedelta(days=30)
    cohort = await session.scalar(select(func.count(User.id)).where(User.created_at <= cutoff)) or 0
    retained = (
        await session.scalar(
            select(func.count(User.id)).where(
                User.created_at <= cutoff, User.last_active_at >= cutoff
            )
        )
        or 0
    )

    kpis = [
        KpiValue(
            key="activation_rate",
            label="Activation rate",
            value=_ratio(onboarded, registered),
            numerator=onboarded,
            denominator=registered,
            target=70.0,
        ),
        KpiValue(
            key="assessment_completion",
            label="Assessment completion",
            value=_ratio(assessed, registered),
            numerator=assessed,
            denominator=registered,
            target=65.0,
        ),
        KpiValue(
            key="plan_adoption",
            label="Plan adoption",
            value=_ratio(plans_accepted, plans_generated),
            numerator=plans_accepted,
            denominator=plans_generated,
            target=60.0,
        ),
        KpiValue(
            key="program_completion",
            label="Program completion",
            value=_ratio(completed, enrolled),
            numerator=completed,
            denominator=enrolled,
        ),
        KpiValue(
            key="opportunity_conversion",
            label="Opportunity conversion",
            value=_ratio(accepted_apps, applications),
            numerator=accepted_apps,
            denominator=applications,
        ),
        KpiValue(
            key="employment_conversion",
            label="Employment conversion",
            value=_ratio(employed, applications),
            numerator=employed,
            denominator=applications,
        ),
        KpiValue(
            key="business_conversion",
            label="Business conversion",
            value=_ratio(businesses, registered),
            numerator=businesses,
            denominator=registered,
        ),
        KpiValue(
            key="commerce_activation",
            label="Commerce activation",
            value=_ratio(sellers, registered),
            numerator=sellers,
            denominator=registered,
        ),
        KpiValue(
            key="retention_d30",
            label="Retention D30",
            value=_ratio(retained, cohort),
            numerator=retained,
            denominator=cohort,
            target=30.0,
        ),
    ]

    return KpiSnapshot(generated_at=datetime.now(UTC), filters=filters, kpis=kpis)


async def dashboard_overview(
    session: AsyncSession, filters: DashboardFilter | None = None
) -> DashboardOverview:
    filters = filters or DashboardFilter()
    thirty_days_ago = datetime.now(UTC) - timedelta(days=30)

    registered = (
        await session.scalar(_apply_user_filters(select(func.count(User.id)), filters)) or 0
    )
    active = (
        await session.scalar(
            _apply_user_filters(
                select(func.count(User.id)).where(User.last_active_at >= thirty_days_ago),
                filters,
            )
        )
        or 0
    )
    onboarded = (
        await session.scalar(
            _apply_user_filters(
                select(func.count(User.id)).where(User.onboarding_completed_at.is_not(None)),
                filters,
            )
        )
        or 0
    )
    completed = (
        await session.scalar(
            select(func.count(Enrollment.id)).where(Enrollment.status == EnrollmentStatus.COMPLETED)
        )
        or 0
    )
    employed = (
        await session.scalar(
            select(func.count(OutcomeRecord.id)).where(
                OutcomeRecord.source == OpportunitySource.EDU_JOB,
                OutcomeRecord.outcome_type == "employment",
            )
        )
        or 0
    )
    businesses = (
        await session.scalar(
            select(func.count(OutcomeRecord.id)).where(
                OutcomeRecord.source == OpportunitySource.INVEST_HUB
            )
        )
        or 0
    )
    sellers = (
        await session.scalar(
            select(func.count(func.distinct(OutcomeRecord.user_id))).where(
                OutcomeRecord.source == OpportunitySource.COMMERCE
            )
        )
        or 0
    )
    mentor_sessions = await session.scalar(select(func.count(MentorSession.id))) or 0
    at_risk = (
        await session.scalar(
            select(func.count(func.distinct(RiskFlag.user_id))).where(
                RiskFlag.resolved_at.is_(None)
            )
        )
        or 0
    )
    avg_score = await session.scalar(select(func.avg(DevelopmentScore.current))) or 0.0

    coverage_rows = await session.execute(
        select(
            User.region,
            func.count(User.id),
            func.count(User.id).filter(User.last_active_at >= thirty_days_ago),
        )
        .where(User.region.is_not(None))
        .group_by(User.region)
    )
    coverage = [
        RegionCoverage(
            region=Region(region),
            registered=total,
            active=active_count,
            avg_development_score=0.0,
        )
        for region, total, active_count in coverage_rows
    ]

    return DashboardOverview(
        total_registered=registered,
        active_users=active,
        onboarding_completed=onboarded,
        programs_completed=completed,
        employed_via_edu_job=employed,
        businesses_via_invest_hub=businesses,
        sellers_via_commerce=sellers,
        mentor_sessions=mentor_sessions,
        at_risk_users=at_risk,
        avg_development_score=round(float(avg_score), 2),
        coverage_by_region=coverage,
    )
