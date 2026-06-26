"""Runnable watcher core.

This step intentionally stops at printing new matches. Email, alumni joins,
and scheduling are later layers.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TextIO

from backend.app.ingest import analyze_rows
from watcher.config import DEFAULT_WATCHLIST_PATH, WatcherConfig, load_watchlist
from watcher.filters import filter_matches
from watcher.seen_store import SeenStore
from watcher.sources import GitHubListingsSource, GreenhouseSource, LeverSource, SourceError

LOGGER = logging.getLogger(__name__)


@dataclass
class RunResult:
    rows_fetched: int
    jobs_scored: int
    matches: list[dict]
    new_matches: list[dict]
    errors: list[str]


def run_once(
    config: WatcherConfig,
    *,
    seen_store: SeenStore,
    direct_sources: dict[str, object] | None = None,
    github_source: object | None = None,
    today: date | None = None,
    seen_at: datetime | None = None,
) -> RunResult:
    rows, errors = collect_rows(config, direct_sources=direct_sources, github_source=github_source)
    jobs = analyze_rows(rows, today=today)
    matches = filter_matches(jobs, target_roles=config.target_roles, min_score=config.min_score)
    new_matches = seen_store.unseen(matches)
    seen_store.mark_many_seen(new_matches, seen_at=seen_at or datetime.now(timezone.utc))
    return RunResult(
        rows_fetched=len(rows),
        jobs_scored=len(jobs),
        matches=matches,
        new_matches=new_matches,
        errors=errors,
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
        if company.ats == "github_only":
            continue
        source = direct_sources.get(company.ats)
        if source is None:
            _record_error(errors, f"{company.name}: no source registered for ats '{company.ats}'")
            continue
        try:
            direct_rows.extend(source.fetch(company))
        except SourceError as exc:
            _record_error(errors, f"{company.name}: {exc}")
        except Exception as exc:  # defensive run-loop boundary
            _record_error(errors, f"{company.name}: unexpected {type(exc).__name__}: {exc}")

    try:
        if hasattr(github_source, "fetch_many"):
            github_rows.extend(github_source.fetch_many(config.companies))
        else:
            for company in config.companies:
                github_rows.extend(github_source.fetch(company))
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
        print(
            f"  score: {score.get('total', 0)} ({score.get('action_label') or score.get('action', 'unknown')})",
            file=output,
        )
        print(f"  top reason: {reasons[0] if reasons else '(none)'}", file=output)
        if red_flags:
            labels = ", ".join(flag.get("label", str(flag)) for flag in red_flags)
            print(f"  red flags: {labels}", file=output)
        else:
            print("  red flags: none", file=output)
        print(f"  url: {job.get('source_url', '')}", file=output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the internship watcher once and print new matches.")
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST_PATH), help="Path to watchlist.yml")
    parser.add_argument("--seen-db", help="Path to SQLite seen-store")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_watchlist(args.watchlist)
    if args.seen_db:
        config = replace(config, seen_db_path=Path(args.seen_db))

    with SeenStore(config.seen_db_path) as seen_store:
        result = run_once(config, seen_store=seen_store)
    print_report(result)
    return 0


def _default_direct_sources() -> dict[str, object]:
    return {
        "greenhouse": GreenhouseSource(),
        "lever": LeverSource(),
    }


def _record_error(errors: list[str], message: str) -> None:
    LOGGER.warning(message)
    errors.append(message)


if __name__ == "__main__":
    raise SystemExit(main())
