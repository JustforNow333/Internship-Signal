"""Per-run company coverage and GitHub row evidence."""

from __future__ import annotations

from typing import Iterable
from typing import Mapping, Sequence

from watcher.company_matching import company_matching_key
from watcher.config import CompanyCfg
from watcher.health.models import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_DEGRADED_BACKSTOP,
    COVERAGE_DIRECT,
    COVERAGE_DIRECT_DEGRADED,
    COVERAGE_DIRECT_EMPTY,
    COVERAGE_FAILING_BACKSTOP,
    COVERAGE_UNCOVERED,
    COVERAGE_UNKNOWN_BACKSTOP,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_EMPTY,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    DIRECT_STATUS_UNKNOWN,
    GITHUB_PRIMARY_ATS,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_UNKNOWN,
    CompanyCoverage,
    SourceAttempt,
    SourceHealthState,
)
from watcher.health.state import direct_health_key

def count_github_rows_by_company(
    rows: Iterable[Mapping[str, object]],
    companies: Sequence[CompanyCfg],
) -> dict[str, int]:
    """Count trusted GitHub rows that map unambiguously to each company."""

    lookup: dict[str, str | None] = {}
    for company in companies:
        for label in (company.name, *tuple(company.aliases)):
            key = company_matching_key(label)
            if not key:
                continue
            if key in lookup and lookup[key] != company.name:
                lookup[key] = None
            else:
                lookup.setdefault(key, company.name)
    counts = {company.name: 0 for company in companies}
    for row in rows:
        extra = row.get("extra")
        if not isinstance(extra, Mapping) or extra.get("source") != "github":
            continue
        matched = lookup.get(company_matching_key(row.get("company")))
        if matched is not None:
            counts[matched] += 1
    return counts


def calculate_company_coverage(
    companies: Sequence[CompanyCfg],
    attempts: Sequence[SourceAttempt],
    states: Mapping[str, SourceHealthState],
    github_rows_by_company: Mapping[str, int] | None = None,
) -> tuple[CompanyCoverage, ...]:
    direct_attempts = {
        attempt.company: attempt
        for attempt in attempts
        if attempt.source_kind == SOURCE_KIND_DIRECT and attempt.company is not None
    }
    github_attempts = [
        attempt
        for attempt in attempts
        if attempt.source_kind == SOURCE_KIND_GITHUB_FEED
    ]
    github_available = any(
        attempt.attempted and attempt.succeeded is True
        for attempt in github_attempts
    ) and not any(attempt.succeeded is False for attempt in github_attempts)
    coverage = []
    for company in companies:
        attempt = direct_attempts.get(company.name)
        key = direct_health_key(company.name, company.ats)
        state = states.get(key)
        direct_status = state.status if state else STATUS_UNKNOWN
        succeeded = attempt.succeeded if attempt else None
        rows = attempt.rows_returned if attempt else None
        if direct_status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS:
            coverage_state = COVERAGE_DIRECT
        elif direct_status == DIRECT_STATUS_HEALTHY_EMPTY:
            coverage_state = COVERAGE_DIRECT_EMPTY
        elif direct_status == DIRECT_STATUS_DEGRADED:
            coverage_state = (
                COVERAGE_DEGRADED_BACKSTOP
                if github_available
                else COVERAGE_DIRECT_DEGRADED
            )
        elif direct_status == DIRECT_STATUS_UNKNOWN and github_available:
            coverage_state = COVERAGE_UNKNOWN_BACKSTOP
        elif company.ats in GITHUB_PRIMARY_ATS and github_available:
            coverage_state = COVERAGE_BACKSTOP_ONLY
        elif direct_status == DIRECT_STATUS_FAILED and github_available:
            coverage_state = COVERAGE_FAILING_BACKSTOP
        else:
            coverage_state = COVERAGE_UNCOVERED
        coverage.append(
            CompanyCoverage(
                company=company.name,
                adapter=company.ats,
                state=coverage_state,
                direct_status=direct_status,
                direct_attempt_succeeded=succeeded,
                direct_rows_returned=rows,
                github_backstop_available=github_available,
                github_rows_returned=(
                    None
                    if github_rows_by_company is None
                    else max(0, int(github_rows_by_company.get(company.name, 0)))
                ),
                github_fallback_configured=(
                    github_available and company.ats not in GITHUB_PRIMARY_ATS
                ),
            )
        )
    return tuple(coverage)
