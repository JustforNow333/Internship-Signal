"""Runnable watcher core.

This step intentionally stops at printing new matches. Scheduling is a later
layer.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

from backend.app.ingest import analyze_rows
from watcher.alumni import AlumniIndex, attach_alumni, load_default_alumni, status_for_injected_index
from watcher.config import DEFAULT_WATCHLIST_PATH, WatcherConfig, load_watchlist
from watcher.filters import filter_matches
from watcher.notify import send_digest
from watcher.seen_store import SeenStore
from watcher.sources import (
    AshbySource,
    GitHubListingsSource,
    GreenhouseSource,
    LeverSource,
    SmartRecruitersSource,
    SourceError,
    WorkableSource,
    WorkdaySource,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class RunResult:
    rows_fetched: int
    jobs_scored: int
    matches: list[dict]
    new_matches: list[dict]
    errors: list[str]
    digest_sent: bool
    seen_marked: int
    alumni_csv_status: str
    alumni_records_loaded: int
    alumni_employers_indexed: int
    alumni_status_message: str = ""


def run_once(
    config: WatcherConfig,
    *,
    seen_store: SeenStore,
    direct_sources: dict[str, object] | None = None,
    github_source: object | None = None,
    alumni_index: AlumniIndex | None = None,
    digest_sender: Callable[[list[dict]], bool] | None = None,
    today: date | None = None,
    seen_at: datetime | None = None,
    mark_seen_without_send: bool = False,
) -> RunResult:
    LOGGER.info("Collecting watcher rows...")
    rows, errors = collect_rows(config, direct_sources=direct_sources, github_source=github_source)
    LOGGER.info("Analyzing %d fetched row(s)...", len(rows))
    jobs = analyze_rows(rows, today=today)
    LOGGER.info("Filtering %d scored job(s)...", len(jobs))
    matches = filter_matches(jobs, target_roles=config.target_roles, min_score=config.min_score)
    if alumni_index is None:
        alumni_index, alumni_status = load_default_alumni()
    else:
        alumni_status = status_for_injected_index(alumni_index)
    LOGGER.info(
        "Alumni CSV status: alumni_csv_status=%s alumni_records_loaded=%d alumni_employers_indexed=%d",
        alumni_status.status,
        alumni_status.records_loaded,
        alumni_status.employers_indexed,
    )
    matches = attach_alumni(
        matches,
        alumni_index,
        companies=config.companies,
    )
    new_matches = seen_store.unseen(matches)
    LOGGER.info("%d match(es), %d new.", len(matches), len(new_matches))
    LOGGER.info("Sending digest if needed...")
    if digest_sender is None:
        digest_sent = send_digest(new_matches, alumni_summary=alumni_status.as_dict())
    else:
        digest_sent = digest_sender(new_matches)
    should_mark_seen = digest_sent or (mark_seen_without_send and bool(new_matches))
    seen_marked = len(new_matches) if should_mark_seen else 0
    if should_mark_seen:
        timestamp = seen_at or datetime.now(timezone.utc)
        seen_store.mark_many_seen(
            new_matches,
            seen_at=timestamp,
            emailed_at=timestamp if digest_sent else None,
        )
        if digest_sent:
            LOGGER.info("Digest sent; marked %d job(s) seen.", seen_marked)
        else:
            LOGGER.info("Digest not sent; priming mode marked %d job(s) seen.", seen_marked)
    else:
        LOGGER.info("Digest not sent; seen-store unchanged.")
    return RunResult(
        rows_fetched=len(rows),
        jobs_scored=len(jobs),
        matches=matches,
        new_matches=new_matches,
        errors=errors,
        digest_sent=digest_sent,
        seen_marked=seen_marked,
        alumni_csv_status=alumni_status.status,
        alumni_records_loaded=alumni_status.records_loaded,
        alumni_employers_indexed=alumni_status.employers_indexed,
        alumni_status_message=alumni_status.message,
    )


def collect_rows(
    config: WatcherConfig,
    *,
    direct_sources: dict[str, object] | None = None,
    github_source: object | None = None,
) -> tuple[list[dict], list[str]]:
    direct_sources = direct_sources or _default_direct_sources()
    github_source = github_source or GitHubListingsSource()
    direct_rows: list[dict] = []
    github_rows: list[dict] = []
    errors: list[str] = []

    for company in config.companies:
        if company.ats in {"bespoke", "github_only"}:
            LOGGER.info("Skipping direct fetch for %s (%s).", company.name, company.ats)
            continue
        source = direct_sources.get(company.ats)
        if source is None:
            _record_error(errors, f"{company.name}: no source registered for ats '{company.ats}'")
            continue
        try:
            LOGGER.info("Fetching %s via %s...", company.name, company.ats)
            rows = source.fetch(company)
            direct_rows.extend(rows)
            LOGGER.info("Fetched %d direct row(s) for %s.", len(rows), company.name)
        except SourceError as exc:
            _record_error(errors, f"{company.name}: {exc}")
        except Exception as exc:  # defensive run-loop boundary
            _record_error(errors, f"{company.name}: unexpected {type(exc).__name__}: {exc}")

    try:
        LOGGER.info("Fetching GitHub listings backstop...")
        if hasattr(github_source, "fetch_many"):
            github_rows.extend(github_source.fetch_many(config.companies))
        else:
            for company in config.companies:
                github_rows.extend(github_source.fetch(company))
        LOGGER.info("Fetched %d GitHub backstop row(s).", len(github_rows))
    except SourceError as exc:
        _record_error(errors, f"github listings: {exc}")
    except Exception as exc:  # defensive run-loop boundary
        _record_error(errors, f"github listings: unexpected {type(exc).__name__}: {exc}")

    # Direct rows first: backend dedupe keeps the first row's extra metadata,
    # so this implements the direct-over-GitHub source-priority rule.
    return [*direct_rows, *github_rows], errors


def print_report(result: RunResult, *, output: TextIO | None = None) -> None:
    output = output or sys.stdout
    if result.errors:
        print(f"Source errors: {len(result.errors)}", file=output)
        for error in result.errors:
            print(f"  - {error}", file=output)

    if not result.new_matches:
        print("No new matches.", file=output)
        return

    print(f"New matches: {len(result.new_matches)}", file=output)
    for job in result.new_matches:
        source = job.get("extra", {}).get("source", "unknown")
        score = job.get("score", {})
        reasons = score.get("reasons") or []
        red_flags = job.get("red_flags") or []
        print(f"[{source}] {job.get('company', '')} - {job.get('title', '')}", file=output)
        print(f"  location: {job.get('location', '') or '(not listed)'}", file=output)
        print(f"  role track: {score.get('role_track') or job.get('role_classification', {}).get('role_track', 'unknown')}", file=output)
        print(
            f"  score: {score.get('total', 0)}, fit: {score.get('fit_score', score.get('total', 0))} "
            f"({score.get('watcher_action_label') or score.get('action_label') or score.get('action', 'unknown')})",
            file=output,
        )
        if score.get("fit_explanation"):
            print(f"  fit reason: {score['fit_explanation']}", file=output)
        print(f"  top reason: {reasons[0] if reasons else '(none)'}", file=output)
        if red_flags:
            labels = ", ".join(flag.get("label", str(flag)) for flag in red_flags)
            print(f"  red flags: {labels}", file=output)
        else:
            print("  red flags: none", file=output)
        print(f"  url: {job.get('source_url', '')}", file=output)


def print_heartbeat(result: RunResult, *, output: TextIO | None = None) -> None:
    output = output or sys.stdout
    sent = "yes" if result.digest_sent else "no"
    print(
        "HEARTBEAT: ran, "
        f"rows_fetched={result.rows_fetched}, "
        f"jobs_scored={result.jobs_scored}, "
        f"matches={len(result.matches)}, "
        f"new={len(result.new_matches)}, "
        f"errors={len(result.errors)}, "
        f"alumni_csv_status={getattr(result, 'alumni_csv_status', 'unknown')}, "
        f"alumni_records_loaded={getattr(result, 'alumni_records_loaded', 0)}, "
        f"alumni_employers_indexed={getattr(result, 'alumni_employers_indexed', 0)}, "
        f"sent={sent}, "
        f"seen_marked={result.seen_marked}",
        file=output,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the internship watcher once and print new matches.")
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST_PATH), help="Path to watchlist.yml")
    parser.add_argument("--seen-db", help="Path to SQLite seen-store")
    parser.add_argument(
        "--mark-seen-without-send",
        action="store_true",
        help="Mark new matches seen even when the digest dry-runs; intended for CI priming.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_watchlist(args.watchlist)
    if args.seen_db:
        config = replace(config, seen_db_path=Path(args.seen_db))

    with SeenStore(config.seen_db_path) as seen_store:
        result = run_once(config, seen_store=seen_store, mark_seen_without_send=args.mark_seen_without_send)
    print_report(result)
    print_heartbeat(result)
    return 0


def _default_direct_sources() -> dict[str, object]:
    return {
        "ashby": AshbySource(),
        "greenhouse": GreenhouseSource(),
        "lever": LeverSource(),
        "smartrecruiters": SmartRecruitersSource(),
        "workable": WorkableSource(),
        "workday": WorkdaySource(),
    }


def _record_error(errors: list[str], message: str) -> None:
    LOGGER.warning(message)
    errors.append(message)


if __name__ == "__main__":
    raise SystemExit(main())
