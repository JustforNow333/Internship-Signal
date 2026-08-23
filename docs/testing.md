# Testing and validation

Which suite to run for which change, and the commands that actually work on this
machine. Everything here is offline.

---

## 1. Ground rules

- **Tests never hit the network.** Adapter tests parse saved fixtures under
  `watcher/tests/fixtures/`; run-loop tests use mocked sources and a fake SMTP.
  Live endpoint verification is a separate manual operation.
- **Health, cache, and snapshot tests use fake sources, fixed timestamps and run
  IDs, and temporary SQLite files.**
- Benchmarking is measurement only: it must not change scoring, and must not use
  alumni data, email, seen state, `watcher-data`, or workflow persistence.
- **Add the regression test before changing behavior.** A bug fix needs a
  reproducible failure or a clearly violated invariant; do not change code for
  style alone.
- Always finish with `git diff --check`.

---

## 2. Targeted suites

Run the narrowest suite that covers the change, then widen if it touches shared
code.

| Change area | Run |
|---|---|
| A single source adapter | `watcher/tests/test_<adapter>.py` (e.g. `test_workday_*.py`, `test_icims.py`, `test_successfactors.py`, `test_paylocity.py`, `test_bain.py`, `test_epic.py`, `test_ibm.py`, `test_sources.py`) |
| Direct-source registry | `watcher/tests/test_source_registry.py` |
| Watchlist / env validation | `watcher/tests/test_config.py` |
| Collection concurrency | `watcher/tests/test_collection_concurrency.py`, `test_canary_collection_concurrency.py` |
| Snapshot capture/replay | `watcher/tests/test_collection_snapshot.py` |
| Static-analysis cache | `watcher/tests/test_analysis_cache.py`, `test_migrate_analysis_cache.py` |
| Eligibility (student/location/role) | `watcher/tests/test_student_eligibility.py`, `test_filters.py` |
| Source health state and sanitizers | `watcher/tests/test_source_health.py`, `test_workday_health_state.py` |
| Health coverage and persistence | `watcher/tests/test_health_coverage.py`, `test_health_store.py`, `test_coverage_audit.py` |
| Health report, workflow output, heartbeat | `watcher/tests/test_health_report.py` |
| Health alerts and orchestration | `watcher/tests/test_health_alerts.py` |
| Health policy, fallback, flapping, grouping | `watcher/tests/test_health_policy.py` |
| Health digest and alert rendering | `watcher/tests/test_health_digest.py` |
| Health facade compatibility | `watcher/tests/test_health_module_exports.py` |
| Audit and source comparison | `watcher/tests/test_audit.py`, `test_source_comparison.py`, `test_coverage_audit.py` |
| Seen store, identity, notification | `watcher/tests/test_seen_store.py`, `test_workday_listing_identity.py`, `backend/tests/test_phase2a_job_identity.py` |
| Digest rendering / email | `watcher/tests/test_notify.py` |
| Alumni matching | `watcher/tests/test_alumni.py`, `test_build_watcher_alumni_map.py`, `test_company_matching.py` |
| Season and rollover | `watcher/tests/test_season.py`, `test_season_terms.py` |
| End-to-end watcher run | `watcher/tests/test_run.py` (pipeline), `test_run_digest.py` |
| Collection, reporting, CLI | `test_run_collection.py`, `test_run_reporting.py`, `test_run_cli.py` |
| Scheduled workflow contract | `test_watcher_workflow.py` |
| Workflow diagnostics / heartbeat | `watcher/tests/test_workflow_diagnostics.py` |
| Backend scoring/classification/signals | `backend/tests/test_scoring.py`, `test_classify.py`, `test_signals.py`, `test_salary.py`, `test_analysis_context.py` |
| Backend ingest / dedupe / normalize | `backend/tests/test_normalize.py`, `test_dedupe.py`, `test_utf8_io.py` |
| Backend API and ask | `backend/tests/test_api.py`, `test_ask.py` |
| Hosted accounts/security | `backend/tests/test_hosted_security.py`, `test_hosted_postgres.py` |
| Hosted import and matching | `backend/tests/test_hosted_job_import.py`, `test_hosted_matching.py`, `test_hosted_match_reconciliation.py` |
| Hosted notifications | `backend/tests/test_hosted_notifications.py`, `test_hosted_notification_mail.py` |
| Frontend | `frontend/src/__tests__/` |
| Benchmark tooling | `watcher/tests/test_scoring_benchmark.py`, `test_us_rolefit_benchmark.py`, `test_us_holdout_benchmark.py` |

Example:

```bash
PYTHONPATH=.:backend backend/venv/Scripts/python.exe -m pytest \
  watcher/tests/test_health_alerts.py -q
```

---

## 3. Full suites

Run the full backend + watcher suite when a change touches `analyze_rows`,
`sources/base.py`, `sources/direct.py`, `watcher/config/`, posting identity, the seen
store, eligibility, scoring, or anything shared across adapters — and before any
commit that will be pushed.

The checked-in Windows virtualenv lives in the primary checkout at
`backend/venv/`.

```bash
PYTHONPATH=.:backend backend/venv/Scripts/python.exe -m pytest backend/tests watcher/tests -q
PYTHONPATH=.:backend python3 -m compileall -q internship_signal backend watcher scripts
```

WSL fallback when inline WSL env assignments do not cross into the Windows
process:

```bash
cmd.exe /C "cd /D C:\Users\burst\internship-signal && set PYTHONPATH=C:\Users\burst\internship-signal;C:\Users\burst\internship-signal\backend && backend\venv\Scripts\python.exe -m pytest backend\tests watcher\tests -q"
```

Frontend:

```bash
cd frontend
npm test
npm run build
```

If WSL Node fails on `/mnt/c`:

```bash
cmd.exe /C "cd /D C:\Users\burst\internship-signal\frontend && npm test -- --run && npm run build"
```

Always finish with:

```bash
git diff --check
git status --short --ignored
```

---

## 4. Benchmarks

Benchmarks are separate from tests and are never required for an ordinary
change. Scoring-benchmark methodology, cohorts, labeling, and metrics live in
[`../evaluation/README.md`](../evaluation/README.md).

```bash
# Analysis context, 500 / 1,000 / 2,000 rows; fails if context-enabled and
# context-free paths differ
PYTHONPATH=.:backend python3 scripts/benchmark_analysis_context.py

# Offline analysis-cache benchmark against temporary SQLite
PYTHONPATH=.:backend python3 scripts/benchmark_analysis_cache.py --rows 2000

# Production-sized replay of the full downstream pipeline, no collection
PYTHONPATH=.:backend python3 scripts/benchmark_static_scoring_cache.py

# Live capture / disabled replay / warm replay
PYTHONPATH=.:backend python3 scripts/benchmark_collection_replay.py

# Warm-replay cost isolation
PYTHONPATH=.:backend python3 scripts/audit_warm_collection_replay.py
```

Rules:

- Benchmark analysis changes offline at 500, 1,000, and 2,000 representative
  rows. Do not add prefilters, collection concurrency, pagination changes, or a
  source-comparison redesign as part of a benchmark.
- Warm-replay audits use benchmark-only instrumentation, a fixed snapshot and
  date, one warm-up, and at least three measured runs. Never add production
  bypass flags to isolate cache, scoring, or source-comparison costs.
- Snapshot benchmarks compare deterministic pipeline outputs and persistent state
  before and after replay; analysis-cache maintenance is replay's only permitted
  write.
- Store benchmark output only in `evaluation/private/`.
- Frozen benchmark artifacts (`scoring_20260724_*`) are immutable. Frozen exports
  keep international rows; current evaluation applies the production eligibility
  helper without regenerating frozen inputs.
- U.S. role-fit benchmarking uses `scripts/build_us_rolefit_benchmark.py` with
  only `us`/`ambiguous` candidates and independent `random`, `likely_match`, and
  `difficult_negative` cohorts. Rebuild those artifacts only from a clean commit,
  validate manifest hashes and `git_dirty=false`, and explain every change from
  the prior export.
- Holdout construction is two-stage: commit the reusable tooling first, then
  collect from that exact clean SHA. Exclude both prior benchmarks by stable ID,
  normalized URL, and fallback key without reading their human labels.
- Benchmark labels require `human_eligible` (`yes`, `no`, `uncertain`); optional
  role track, exclusion reason, and notes do not affect binary metrics.
- Benchmark export uses `collect_rows()` then `analyze_rows()`; candidates use
  only `is_internship()` and `is_open()`, never `filter_matches()`.

---

## 5. Guard tests worth knowing

- `STATIC_ANALYSIS_CACHE_VERSION` is pinned by a guard test. Any
  static-eligibility, classification, or artifact change must bump it, or that
  test fails — see [`watcher.md`](watcher.md#12-persistent-static-analysis-cache).
- Serial and concurrent collection must produce byte-identical batches,
  snapshots, ordering, limits, isolation, pacing, and zero state writes.
- Cache-enabled and cache-disabled runs must produce byte-identical jobs and
  dedupe reports.
- Context-enabled and context-free analysis must serialize identically.
