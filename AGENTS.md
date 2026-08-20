# AGENTS.md

Canonical instructions for coding agents. Read this first; it should be enough to
orient yourself and find the one or two documents your task needs.

## Project

Internship Signal scores internship postings. A FastAPI backend + React frontend
handle CSV ingestion and the local UI; the scheduled `watcher` package collects
postings from ATS APIs and GitHub community feeds and emails a digest of new
matches; a hosted multi-user layer lives under `backend/app/hosted/`.

**Branches** ([`BRANCH_STRATEGY.md`](BRANCH_STRATEGY.md)): `internal-tool`
(default, personal/internal), `product-mvp` (hosted product, Phase 3B paused),
`main` (shared validated baseline), `watcher-data` (orphan, holds only
`seen.sqlite`). Never merge `internal-tool` and `product-mvp` wholesale —
cherry-pick shared fixes.

## Repository map

| Path | Owns |
|---|---|
| `backend/app/ingest.py` | `process_csv` (CSV cleaning) and `analyze_rows` (shared analysis seam) |
| `backend/app/` | normalize, dedupe, salary, classify, signals, scoring, eligibility, ask, profile, config, store |
| `backend/app/hosted/` | accounts, PostgreSQL job import, per-user matching, notification delivery |
| `watcher/run.py` | `python -m watcher.run` entry point and the `watcher.run` compatibility exports |
| `watcher/pipeline.py` | `run_once`: collect → analyze → filter → seen partition → digest → health |
| `watcher/collection.py` | direct/GitHub fetch planning, outcomes, source attempts, Workday counters |
| `watcher/reporting.py` · `cli.py` | console report + heartbeat · argparse and startup |
| `watcher/config.py` | watchlist + env validation; `supported_ats()` derives from the registry |
| `watcher/sources/registry.py` | **canonical direct-source registry** — register a new ATS adapter here only |
| `watcher/sources/` | one adapter per ATS and per GitHub backstop format |
| `watcher/eligibility.py` · `filters.py` | student/location/role gates · internship, open, min-score |
| `watcher/seen_store.py` · `analysis_cache.py` | durable `seen.sqlite` · rebuildable cache + `STATIC_ANALYSIS_CACHE_VERSION` |
| `watcher/source_health.py` · `health_alerts.py` | health state · severity routing and daily digest |
| `watcher/audit*.py` · `source_comparison.py` | read-only observability |
| `frontend/src/` · `scripts/` · `evaluation/` | local + hosted UI · probes, benchmarks, migrations · benchmark tooling |

## Non-negotiable rules

1. **Change only the layer requested.** Preserve unrelated work and every
   pre-existing dirty hunk; never stash, reset, clean, or touch another worktree.
2. **Backend owns analysis.** The watcher never computes scores, role tracks, or
   job IDs and never duplicates dedupe/classification/signals. Reuse
   `analyze_rows`, `norm_company`, `norm_url` verbatim, and never treat the
   content-hash `job["id"]` as an ATS requisition ID.
3. **Adapters only fetch canonical rows.** Eligibility lives in
   `watcher/eligibility.py`; `filters.py` adds internship/open/min-score.
4. **Bump `STATIC_ANALYSIS_CACHE_VERSION`** for any static-eligibility,
   classification, or cached-artifact change. A guard test pins the value.
5. **Ambiguity passes.** Student-status and location exclusions need clear
   mandatory evidence; mixed, preferred, incidental, and ambiguous evidence stays
   eligible. `assess_us_location` is the sole location gate.
6. **One posting-identity policy** for dedupe and notification suppression:
   stable requisition ID → posting-specific normalized URL → exact
   company/title/location. Generic URLs never collapse distinct requisitions.
7. **Never weaken source safety.** No proxies, cookie harvesting, header
   rotation, browser automation, or challenge bypass. Blocked, unauthorized, and
   rate-limited responses are ordinary source failures; HTML is a fetch failure,
   never an empty board.
8. **Sanitizers are total** and never raise. Never log or persist payloads,
   credentials, raw query strings, alumni contacts, or SMTP recipients.
9. **Dry runs, audits, and replay change nothing** — no email, priming, seen
   marking, or health/comparison persistence. Health alerting never touches
   internship `emailed_at` or `primed_at`.
10. **Concurrency reorders nothing.** Opt-in and bounded; application default
    `serial`, scheduled production `concurrent` at `4/1/2`.
11. **JSON endpoints go through `_json_object`**: malformed or wrongly shaped
    requests are HTTP 400, never internal errors. Text I/O is explicit UTF-8.
12. **Alumni data is additive and private** — never gates, reorders, or rescores.
13. **Never commit** `.env`, credentials, alumni data, SQLite state, snapshots,
    probe/health output, Actions diagnostics, profiler data, or
    `evaluation/private/`. Reusable scripts, tests, and fixtures stay tracked.
14. **Tests and benchmarks stay offline.** Change behavior only for a reproduced
    failure or clearly violated invariant, regression test first, no style-only
    edits.
15. **Commit and push only when explicitly asked.** Push a dedicated branch by
    its explicit name, never force-push, and confirm with `ls-remote` that the
    remote matches the exact local SHA.
16. When a rule changes, update this file, keep `claude.md` a short pointer to
    it, and keep `.gitignore` in sync for new generated artifacts.

## Task routing

Docs referenced below: [`docs/watcher.md`](docs/watcher.md) (W),
[`docs/watcher-sources.md`](docs/watcher-sources.md),
[`docs/architecture.md`](docs/architecture.md),
[`docs/operations.md`](docs/operations.md),
[`docs/testing.md`](docs/testing.md).

| Task | Read | Test |
|---|---|---|
| ATS adapter | `watcher-sources.md`, `sources/registry.py`, `sources/base.py` | `watcher/tests/test_<adapter>.py`, `test_source_registry.py` |
| Workday transport/retry/pacing | W §4 | `test_workday_*.py` |
| Watchlist / env validation | W §2 | `test_config.py` |
| Concurrency, snapshot, replay | W §11, §13 | `test_collection_concurrency.py`, `test_collection_snapshot.py` |
| Analysis cache | W §12 | `test_analysis_cache.py` |
| Eligibility, dedupe, identity | W §7–§9 | `test_student_eligibility.py`, `test_filters.py`, `test_seen_store.py` |
| Source health and alerts | W §14–§15 | `test_source_health.py`, `test_health_alerts.py` |
| Audit, coverage, comparison | W §16 | `test_audit.py`, `test_source_comparison.py`, `test_coverage_audit.py` |
| Scoring, classification, signals, ask | `architecture.md` | `backend/tests/test_scoring.py`, `test_classify.py`, `test_signals.py`, `test_ask.py` |
| Hosted backend and notifications | [`backend/HOSTED_BACKEND.md`](backend/HOSTED_BACKEND.md) | `backend/tests/test_hosted_*.py` |
| Frontend | [`frontend/HOSTED_API.md`](frontend/HOSTED_API.md) for hosted mode | `cd frontend && npm test && npm run build` |
| Actions, env, state, probes, rollover | `operations.md` | — |
| Scoring benchmarks | [`evaluation/README.md`](evaluation/README.md) | `test_scoring_benchmark.py`, `test_us_*_benchmark.py` |

## Tests

Run the narrowest suite covering the change; run the full backend + watcher suite
when touching `analyze_rows`, `sources/base.py`, `sources/direct.py`,
`config.py`, posting identity, the seen store, eligibility, or scoring.

```bash
PYTHONPATH=.:backend backend/venv/Scripts/python.exe -m pytest backend/tests watcher/tests -q
PYTHONPATH=.:backend python3 -m compileall -q backend watcher scripts
cd frontend && npm test && npm run build
git diff --check
```

WSL fallback commands and benchmark rules: [`docs/testing.md`](docs/testing.md).

## Historical material

`docs/history/` holds the completed watcher implementation log. **Do not read it
to learn how the system works** — the docs above supersede it. Open it only when
you need historical context (why a decision was made, when a behavior was
introduced). Where it disagrees with current code, the code wins.
