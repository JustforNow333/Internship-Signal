"""Per-company coverage for one run, and the configuration coverage audit.

``calculate_company_coverage`` answers "was this company covered on this run";
``build_coverage_audit`` answers "is this company configured to be coverable at
all". Both are pure and neither fetches anything.
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Sequence, TextIO

from watcher.company_matching import company_matching_key
from watcher.config import (
    COVERAGE_STATUS_NO_SOURCE_FOUND,
    CompanyCfg,
    WatcherConfig,
)
from watcher.health.models import (
    COVERAGE_AUDIT_BACKSTOP_ONLY,
    COVERAGE_AUDIT_DIRECT_DEGRADED,
    COVERAGE_AUDIT_DIRECT_VERIFIED,
    COVERAGE_AUDIT_NEEDS_INVESTIGATION,
    COVERAGE_AUDIT_NO_SOURCE_FOUND,
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
    STATUS_EMPTY,
    STATUS_HEALTHY,
    STATUS_UNKNOWN,
    CompanyCoverage,
    SourceAttempt,
    SourceHealthState,
)
from watcher.health.state import direct_health_key


@dataclass(frozen=True)
class CompanyCoverageAudit:
    """One configuration-and-health coverage classification."""

    company: str
    ats: str
    state: str
    direct_health_status: str | None
    platform_family: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PlatformCoverageGap:
    platform_family: str
    companies: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "platform_family": self.platform_family,
            "companies": list(self.companies),
        }


@dataclass(frozen=True)
class CoverageAuditReport:
    """Deterministic, bounded company-source coverage report."""

    companies: tuple[CompanyCoverageAudit, ...]

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

    @property
    def accounted_coverage_percentage(self) -> float:
        investigated = (
            self.total_companies
            - self.state_counts[COVERAGE_AUDIT_NEEDS_INVESTIGATION]
        )
        return _coverage_percentage(investigated, self.total_companies)

    @property
    def needs_investigation(self) -> tuple[str, ...]:
        return tuple(
            company.company
            for company in self.companies
            if company.state == COVERAGE_AUDIT_NEEDS_INVESTIGATION
        )

    @property
    def degraded_direct_sources(self) -> tuple[CompanyCoverageAudit, ...]:
        return tuple(
            company
            for company in self.companies
            if company.state == COVERAGE_AUDIT_DIRECT_DEGRADED
        )

    @property
    def platform_gaps(self) -> tuple[PlatformCoverageGap, ...]:
        grouped: dict[str, list[str]] = {}
        for company in self.companies:
            if not company.platform_family:
                continue
            grouped.setdefault(company.platform_family, []).append(company.company)
        return tuple(
            PlatformCoverageGap(
                platform_family=platform,
                companies=tuple(sorted(companies, key=_coverage_sort_key)),
            )
            for platform, companies in sorted(
                grouped.items(), key=lambda item: _coverage_sort_key(item[0])
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "report_type": "company_source_coverage",
            "total_companies": self.total_companies,
            "state_counts": self.state_counts,
            "state_percentages": self.state_percentages,
            "direct_coverage_percentage": self.direct_coverage_percentage,
            "accounted_coverage_percentage": self.accounted_coverage_percentage,
            "needs_investigation": list(self.needs_investigation),
            "degraded_direct_sources": [
                {
                    "company": company.company,
                    "ats": company.ats,
                    "direct_health_status": company.direct_health_status,
                }
                for company in self.degraded_direct_sources
            ],
            "platform_gaps": [gap.as_dict() for gap in self.platform_gaps],
            "companies": [company.as_dict() for company in self.companies],
        }


def count_github_rows_by_company(
    rows: Iterable[Mapping[str, object]],
    companies: Sequence[CompanyCfg],
) -> dict[str, int]:
    """Count GitHub-sourced collection rows that map onto each company.

    This reads only rows produced by this run's adapters, where ``make_row``
    always sets ``extra['source']``. Labels are resolved through the watchlist
    matching key exactly once per row, and an ambiguous label that several
    companies claim is attributed to none of them.
    """

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
        attempt for attempt in attempts if attempt.source_kind == SOURCE_KIND_GITHUB_FEED
    ]
    # A backstop is available only when the whole backstop answered. Per-company
    # GitHub evidence is an aggregate row count that does not record which feed
    # supplied it, so one failed feed leaves it unknown whether the surviving
    # feeds still carry any given company. Ambiguity fails closed.
    github_available = any(
        attempt.attempted and attempt.succeeded is True for attempt in github_attempts
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
) -> CoverageAuditReport:
    """Classify configured companies without collection or state mutation.

    GitHub configuration establishes intentional backstop reliance only. Feed
    health and row counts are deliberately ignored because a global feed's
    success is not evidence that it currently lists any particular company.
    """

    companies = tuple(getattr(config, "companies", ()))
    has_github_backstop = bool(config.effective_github_listing_sources())
    audited: list[CompanyCoverageAudit] = []
    for company in companies:
        health = states.get(direct_health_key(company.name, company.ats))
        health_status = health.status if health is not None else None
        no_direct_source = company.ats in {"bespoke", "github_only"}
        if company.coverage_status == COVERAGE_STATUS_NO_SOURCE_FOUND:
            coverage_state = COVERAGE_AUDIT_NO_SOURCE_FOUND
        elif no_direct_source:
            coverage_state = (
                COVERAGE_AUDIT_BACKSTOP_ONLY
                if has_github_backstop
                else COVERAGE_AUDIT_NEEDS_INVESTIGATION
            )
        elif health is None:
            coverage_state = COVERAGE_AUDIT_NEEDS_INVESTIGATION
        elif health_status in {
            DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
            DIRECT_STATUS_HEALTHY_EMPTY,
            # Persisted databases from before the explicit direct-state names
            # still contain trustworthy successful collection evidence.
            STATUS_HEALTHY,
            STATUS_EMPTY,
        }:
            coverage_state = COVERAGE_AUDIT_DIRECT_VERIFIED
        else:
            coverage_state = COVERAGE_AUDIT_DIRECT_DEGRADED

        platform_family = company.platform_family
        if no_direct_source and company.ats == "bespoke" and not platform_family:
            platform_family = "Bespoke / unspecified"
        audited.append(
            CompanyCoverageAudit(
                company=company.name,
                ats=company.ats,
                state=coverage_state,
                direct_health_status=health_status,
                platform_family=platform_family,
            )
        )
    return CoverageAuditReport(
        companies=tuple(sorted(audited, key=lambda item: _coverage_sort_key(item.company)))
    )


def render_coverage_audit(
    report: CoverageAuditReport,
    *,
    output: TextIO | None = None,
) -> None:
    """Render the bounded human-readable coverage audit."""

    stream = output or sys.stdout
    labels = {
        COVERAGE_AUDIT_DIRECT_VERIFIED: "Direct verified",
        COVERAGE_AUDIT_DIRECT_DEGRADED: "Direct degraded",
        COVERAGE_AUDIT_BACKSTOP_ONLY: "Backstop only",
        COVERAGE_AUDIT_NO_SOURCE_FOUND: "No source found",
        COVERAGE_AUDIT_NEEDS_INVESTIGATION: "Needs investigation",
    }
    print("Coverage Audit", file=stream)
    print("", file=stream)
    print(f"Total companies:        {report.total_companies}", file=stream)
    for state in COVERAGE_AUDIT_STATES:
        count = report.state_counts[state]
        percentage = report.state_percentages[state]
        print(f"{labels[state] + ':':24}{count:5d} ({percentage:.1f}%)", file=stream)
    print("", file=stream)
    print(f"Direct coverage:       {report.direct_coverage_percentage:.1f}%", file=stream)
    print(f"Accounted coverage:    {report.accounted_coverage_percentage:.1f}%", file=stream)
    print("", file=stream)
    _render_company_names(
        "Needs investigation",
        report.needs_investigation,
        stream,
    )
    print("", file=stream)
    print("Degraded direct sources:", file=stream)
    if not report.degraded_direct_sources:
        print("  (none)", file=stream)
    for company in report.degraded_direct_sources:
        print(
            f"  - {company.company} ({company.ats}: "
            f"{company.direct_health_status or 'unknown'})",
            file=stream,
        )
    print("", file=stream)
    print("Unsupported/platform gaps:", file=stream)
    if not report.platform_gaps:
        print("  (none)", file=stream)
    for gap in report.platform_gaps:
        print(f"  {gap.platform_family}:", file=stream)
        for company in gap.companies:
            print(f"    - {company}", file=stream)


def _render_company_names(label: str, companies: Sequence[str], output: TextIO) -> None:
    print(f"{label}:", file=output)
    if not companies:
        print("  (none)", file=output)
        return
    for company in companies:
        print(f"  - {company}", file=output)


def _coverage_percentage(count: int, total: int) -> float:
    return round((count * 100.0 / total) if total else 0.0, 1)


def _coverage_sort_key(value: str) -> tuple[str, str]:
    return value.casefold(), value
