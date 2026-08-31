"""Per-run coverage evidence and read-only product coverage reporting."""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Sequence, TextIO

from watcher.company_matching import company_matching_key
from watcher.config import CompanyCfg, WatcherConfig
from watcher.health.models import (
    COVERAGE_AUDIT_BACKSTOP_ONLY,
    COVERAGE_AUDIT_DIRECT_DEGRADED,
    COVERAGE_AUDIT_DIRECT_UNVERIFIED,
    COVERAGE_AUDIT_DIRECT_VERIFIED,
    COVERAGE_AUDIT_NEEDS_INVESTIGATION,
    COVERAGE_AUDIT_STATES,
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
    STATUS_DEGRADED,
    STATUS_EMPTY,
    STATUS_FAILING,
    STATUS_HEALTHY,
    STATUS_UNKNOWN,
    CompanyCoverage,
    SourceAttempt,
    SourceHealthState,
)
from watcher.health.state import direct_health_key


@dataclass(frozen=True)
class CompanyCoverageAudit:
    """One configured company's persisted-evidence classification."""

    company: str
    ats: str
    state: str
    direct_health_status: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CoverageAuditReport:
    """Deterministic, bounded source coverage for the hosted product."""

    companies: tuple[CompanyCoverageAudit, ...]
    state_database_present: bool

    @property
    def total_companies(self) -> int:
        return len(self.companies)

    @property
    def state_counts(self) -> dict[str, int]:
        return {
            state: sum(company.state == state for company in self.companies)
            for state in COVERAGE_AUDIT_STATES
        }

    @property
    def state_percentages(self) -> dict[str, float]:
        return {
            state: _coverage_percentage(count, self.total_companies)
            for state, count in self.state_counts.items()
        }

    @property
    def direct_coverage_percentage(self) -> float:
        return _coverage_percentage(
            self.state_counts[COVERAGE_AUDIT_DIRECT_VERIFIED],
            self.total_companies,
        )

    def companies_in_state(self, state: str) -> tuple[CompanyCoverageAudit, ...]:
        return tuple(company for company in self.companies if company.state == state)

    def as_dict(self) -> dict[str, object]:
        grouped = {
            "verified_direct_sources": COVERAGE_AUDIT_DIRECT_VERIFIED,
            "degraded_direct_sources": COVERAGE_AUDIT_DIRECT_DEGRADED,
            "unverified_direct_sources": COVERAGE_AUDIT_DIRECT_UNVERIFIED,
            "backstop_only_companies": COVERAGE_AUDIT_BACKSTOP_ONLY,
            "needs_investigation": COVERAGE_AUDIT_NEEDS_INVESTIGATION,
        }
        return {
            "schema_version": 1,
            "report_type": "product_source_coverage",
            "state_database_present": self.state_database_present,
            "total_companies": self.total_companies,
            "state_counts": self.state_counts,
            "state_percentages": self.state_percentages,
            "direct_coverage_percentage": self.direct_coverage_percentage,
            **{
                name: [item.as_dict() for item in self.companies_in_state(state)]
                for name, state in grouped.items()
            },
            "companies": [company.as_dict() for company in self.companies],
        }


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


def build_coverage_audit(
    config: WatcherConfig,
    states: Mapping[str, SourceHealthState],
    *,
    state_database_present: bool,
) -> CoverageAuditReport:
    """Classify configured coverage solely from config and persisted health.

    A configured direct adapter is verified only by a trustworthy successful
    direct-source state. GitHub feed health is deliberately ignored because a
    global feed cannot prove that it currently lists any particular company.
    """

    # Keep the registry import deferred: loading the health package itself must
    # not import the direct adapter graph.
    from watcher.sources.registry import DIRECT_ATS

    has_github_backstop = bool(config.effective_github_listing_sources())
    healthy_statuses = {
        DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        DIRECT_STATUS_HEALTHY_EMPTY,
        STATUS_HEALTHY,
        STATUS_EMPTY,
    }
    degraded_statuses = {
        DIRECT_STATUS_DEGRADED,
        DIRECT_STATUS_FAILED,
        STATUS_DEGRADED,
        STATUS_FAILING,
    }
    audited: list[CompanyCoverageAudit] = []
    for company in config.companies:
        health_status: str | None = None
        if company.ats in DIRECT_ATS:
            health = states.get(direct_health_key(company.name, company.ats))
            health_status = health.status if health is not None else None
            if health_status in healthy_statuses:
                coverage_state = COVERAGE_AUDIT_DIRECT_VERIFIED
            elif health_status in degraded_statuses:
                coverage_state = COVERAGE_AUDIT_DIRECT_DEGRADED
            else:
                # Missing, unknown, and not-configured states have not supplied
                # trustworthy evidence for the configured adapter.
                coverage_state = COVERAGE_AUDIT_DIRECT_UNVERIFIED
        elif company.ats in GITHUB_PRIMARY_ATS and has_github_backstop:
            coverage_state = COVERAGE_AUDIT_BACKSTOP_ONLY
        else:
            coverage_state = COVERAGE_AUDIT_NEEDS_INVESTIGATION
        audited.append(
            CompanyCoverageAudit(
                company=company.name,
                ats=company.ats,
                state=coverage_state,
                direct_health_status=health_status,
            )
        )

    return CoverageAuditReport(
        companies=tuple(
            sorted(audited, key=lambda item: _coverage_sort_key(item.company))
        ),
        state_database_present=bool(state_database_present),
    )


def render_coverage_audit(
    report: CoverageAuditReport,
    *,
    output: TextIO | None = None,
) -> None:
    """Render the deterministic product coverage summary."""

    stream = output or sys.stdout
    labels = {
        COVERAGE_AUDIT_DIRECT_VERIFIED: "Verified healthy direct coverage",
        COVERAGE_AUDIT_DIRECT_DEGRADED: "Degraded/failing direct coverage",
        COVERAGE_AUDIT_DIRECT_UNVERIFIED: "Direct coverage without evidence",
        COVERAGE_AUDIT_BACKSTOP_ONLY: "Intentional backstop-only coverage",
        COVERAGE_AUDIT_NEEDS_INVESTIGATION: "Needs source investigation",
    }
    database_status = "present" if report.state_database_present else "absent"
    print("Product Source Coverage Audit", file=stream)
    print(f"State database: {database_status}", file=stream)
    print(f"Total companies: {report.total_companies}", file=stream)
    for state in COVERAGE_AUDIT_STATES:
        print("", file=stream)
        print(f"{labels[state]} ({report.state_counts[state]}):", file=stream)
        entries = report.companies_in_state(state)
        if not entries:
            print("  (none)", file=stream)
            continue
        for item in entries:
            detail = item.ats
            if item.direct_health_status:
                detail += f", {item.direct_health_status}"
            print(f"  - {item.company} ({detail})", file=stream)


def _coverage_percentage(count: int, total: int) -> float:
    return round((count * 100.0 / total) if total else 0.0, 1)


def _coverage_sort_key(value: str) -> tuple[str, str]:
    return value.casefold(), value
