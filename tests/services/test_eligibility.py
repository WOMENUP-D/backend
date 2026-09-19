"""Eligibility: three answers, never rounded up.

Pure logic — no database. Pins that a closed listing is never open to anyone,
that an age rule a listing states is enforced both ways, that an unknown age is
"unknown" rather than "eligible" wherever a listing states an age rule, that a
girl known to be under 18 is not offered adult listings, and that a
requirement the platform cannot read is reported rather than ignored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.constants import EligibilityStatus, OpportunitySource, OpportunityType
from app.models.opportunity import Opportunity
from app.services import eligibility

NOW = datetime(2026, 9, 18, 12, tzinfo=UTC)


def listing(*, rules: dict | None = None, days: int | None = 10, active: bool = True):
    return Opportunity(
        source=OpportunitySource.EDU_JOB,
        type=OpportunityType.VACANCY,
        title_i18n={"uz": "Buxgalter"},
        required_skills=[],
        eligibility=rules or {},
        reward={},
        deadline=NOW + timedelta(days=days) if days is not None else None,
        is_active=active,
    )


def test_a_closed_listing_is_open_to_nobody():
    for item in (listing(days=-1), listing(active=False)):
        verdict = eligibility.check(item, age=30, now=NOW)
        assert verdict.status == EligibilityStatus.NOT_ELIGIBLE
        assert verdict.reason == "closed"
        assert not verdict.may_apply


def test_a_listing_with_no_deadline_stays_open():
    assert eligibility.is_open(listing(days=None), NOW)


def test_with_no_rule_an_adult_or_an_unknown_age_may_apply():
    for age in (18, 45, None):
        verdict = eligibility.check(listing(), age=age, now=NOW)
        assert verdict.status == EligibilityStatus.ELIGIBLE
        assert verdict.may_apply


def test_a_girl_known_to_be_under_18_is_not_offered_an_adult_listing():
    verdict = eligibility.check(listing(), age=15, now=NOW)
    assert verdict.status == EligibilityStatus.NOT_ELIGIBLE
    assert verdict.reason == "adults_only"
    assert eligibility.excluded(listing(), age=15, now=NOW)


def test_a_stated_age_rule_is_enforced_both_ways():
    youth = listing(rules={"age_min": 16, "age_max": 30})
    assert eligibility.check(youth, age=16, now=NOW).status == EligibilityStatus.ELIGIBLE
    assert eligibility.check(youth, age=15, now=NOW).reason == "too_young"
    assert eligibility.check(youth, age=31, now=NOW).reason == "too_old"


def test_an_unknown_age_against_an_age_rule_is_unknown_and_blocks_applying():
    verdict = eligibility.check(listing(rules={"age_min": 18}), age=None, now=NOW)
    assert verdict.status == EligibilityStatus.UNKNOWN
    assert verdict.reason == "age_unknown"
    assert not verdict.may_apply
    # Unknown is not a definite no: the listing is still shown, with the reason.
    assert not eligibility.excluded(listing(rules={"age_min": 18}), age=None, now=NOW)


def test_a_requirement_the_platform_cannot_read_is_reported_not_ignored():
    item = listing(rules={"education": "higher"})
    verdict = eligibility.check(item, age=30, now=NOW)
    assert verdict.status == EligibilityStatus.UNKNOWN
    assert verdict.reason == "other_requirements"
    # The organisation reads it; it does not stop her applying.
    assert verdict.may_apply
    assert eligibility.unreadable(item) == ["education"]


def test_age_limits_are_read_only_from_whole_numbers():
    assert eligibility.age_limits(listing(rules={"age_min": "21"})) == (21, None)
    assert eligibility.age_limits(listing(rules={"age_min": True, "age_max": "abc"})) == (
        None,
        None,
    )
