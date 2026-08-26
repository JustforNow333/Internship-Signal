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
| `internship_signal/domain/` | **neutral shared layer** — canonical job schema, identity normalizers, shared eligibility reason codes; depends on neither layer |
| `backend/app/ingest.py` | `process_csv` (CSV cleaning) and `analyze_rows` (shared analysis seam) |
| `backend/app/` | normalize, dedupe, salary, classify, signals, scoring, eligibility, ask, profile, config, store |
| `backend/app/hosted/` | accounts, PostgreSQL job import, per-user matching, notification delivery |
| `watcher/run.py` | stable entry point and compatibility facade; no new implementation |
| `watcher/pipeline.py` · `collection.py` | `run_once` orchestration · source collection execution and outcomes |
| `watcher/reporting.py` · `cli.py` · `run_logging.py` | reports/heartbeat · startup · stable logger/timing |
| `watcher/config/` | `env.py` (dotenv + every `WATCHER_*` setting) · `models.py` (config dataclasses) · `loader.py` (watchlist file + YAML → config objects) · `validation.py` (pure watchlist rules) · `__init__.py` (stable facade); `supported_ats()` derives from the registry |
| `watcher/text_safety.py` | dependency-free safe text/exception conversion for failure-path diagnostics |
| `watcher/sources/registry.py` | **canonical direct-source registry** — register a new ATS adapter here only |
| `watcher/sources/` | one adapter per ATS and per GitHub backstop format |
| `watcher/sources/contracts.py` · `transport.py` · `parsing.py` · `rows.py` · `sanitize.py` · `diagnostics.py` · `retry.py` | the shared source layer, split by responsibility; `base.py` is a re-export facade |
| `watcher/eligibility.py` · `filters.py` | student/location/role gates · internship, open, min-score |
| `watcher/seen_store.py` · `analysis_cache.py` | durable `seen.sqlite` · rebuildable cache + `STATIC_ANALYSIS_CACHE_VERSION` |
| `watcher/health/` | health models, state, coverage, policy, store, rendering, service, report |
| `watcher/source_health.py` · `health_alerts.py` | the two `watcher.health` compatibility facades |
| `watcher/audit*.py` · `source_comparison.py` | read-only observability |
| `frontend/src/` · `scripts/` · `evaluation/` | local + hosted UI · probes, benchmarks, migrations · benchmark tooling |

## Architecture and ownership

Detailed design lives in [`docs/architecture.md`](docs/architecture.md),
[`docs/watcher.md`](docs/watcher.md), and
[`docs/watcher-sources.md`](docs/watcher-sources.md). The rules below are the
short ownership map; current code wins over historical documentation.

### Neutral domain

`internship_signal/domain/` is the dependency-neutral shared layer:

- `jobs.py` owns the canonical shared job schema;
- `identity.py` owns shared identity normalization primitives;
- `eligibility.py` owns shared categorical eligibility definitions.

Imports flow `watcher → domain ← backend`; the domain must never import either
higher layer. Move only genuinely shared primitives there. Backend scoring,
posting-identity policy, dedupe orchestration, APIs, persistence, and watcher
operations stay with their current owners even when their code is pure.

### Watcher configuration

In `watcher/config/`, `models.py` owns dataclasses, `env.py` owns dotenv and
environment coercion, `loader.py` owns YAML/watchlist loading and construction,
and `validation.py` owns validation. `__init__.py` is the stable compatibility
facade: put new behavior in its owning module, not in the facade.

### Source layer

In `watcher/sources/`, ownership is:

- `contracts.py`: source/response contracts and source exceptions;
- `diagnostics.py`: `DirectSourceDiagnostics` and shared machinery;
- `transport.py`: HTTP transport, decoding, and response/error classification;
- `parsing.py`, `rows.py`, `sanitize.py`: shared parsing, canonical row
  construction, and total source diagnostic sanitization;
- `retry.py`: the narrow shared bounded-retry primitive;
- `registry.py`: the only direct-source adapter registry;
- `base.py`: compatibility re-exports only;
- provider modules: provider-specific fetch, pagination, parsing, completeness,
  retry interpretation, and diagnostics.

Canonical internal source modules must not import `base.py`, and `base.py` must
not gain implementation logic. Adapters publish `DirectSourceDiagnostics`;
collection consumes that contract and never provider-private fields. Extend a
shared primitive only when semantics match: do not force Workday or another
distinct retry contract into `retry.py`, and do not create a universal adapter
framework without concrete need.

`watcher/sources/__init__.py` is a lazy compatibility facade. Preserve its
explicit exports and `__getattr__` resolution/caching; do not eagerly load all
adapters or import `registry.py` to populate exports. Package-level additions
need a real compatibility use case, and leaf imports such as `sanitize`,
`contracts`, and `retry` must remain lightweight.

### Watcher execution and health

`collection.py` owns collection execution, provider handling, attempts, and
collection diagnostics/stats; `pipeline.py` owns `run_once`; `reporting.py`
owns console/heartbeat output; `cli.py` owns startup; `run_logging.py` owns the
stable logger and timing helper. `run.py` is only the stable entry point and
compatibility facade. Patch the module that owns and uses a binding, not a
re-export in `run.py`.

In `watcher/health/`, `models.py` owns records/constants, `sanitize.py` total
sanitizers, `state.py` transitions, `coverage.py` coverage, `policy.py` alert
policy, `store.py` persistence, `rendering.py` formatting, `service.py`
orchestration/SMTP, and `report.py` CLI-facing reports. `source_health.py` and
`health_alerts.py` are compatibility facades. New behavior belongs in the
canonical `watcher.health.*` owner; preserve reason codes, schemas, alert
semantics, and compatibility unless behavior change is explicitly in scope.

`watcher/text_safety.py` owns small dependency-free conversion primitives for
failure paths. Diagnostic code must tolerate hostile `__bool__` and `__str__`;
keep this module narrow rather than making it a generic utility collection.

## Non-negotiable rules

1. **Change only the layer requested.** Preserve unrelated work and every
   pre-existing dirty hunk; never stash, reset, clean, or touch another worktree.
2. **Backend owns analysis.** The watcher never computes scores, role tracks, or
   job IDs and never duplicates dedupe/classification/signals. Reuse
   `analyze_rows` and the posting-identity keys from `backend/app/dedupe.py`
   verbatim, and never treat the content-hash `job["id"]` as an ATS requisition
   ID. Concepts genuinely shared by both layers — `CANONICAL_COLUMNS`,
   `norm_company`/`norm_title`/`norm_url`, `CATEGORICAL_EXCLUSION_REASONS` —
   are owned by `internship_signal/domain/`, which both layers import and which
   imports neither. Watcher code must not reach into `backend.app` for those;
   `backend.app` re-exports them for existing callers.
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

## When adding or changing code

1. Put implementation in its semantic owner; compatibility facades expose APIs
   and do not own new behavior.
2. Search for the canonical owner, registry, contract, or narrow abstraction
   before creating one.
3. Extend an abstraction only when semantics genuinely match. Keep distinct
   provider behavior local instead of generalizing only to remove duplication.
4. Maintain dependency direction: lower layers never depend on orchestration or
   compatibility facades.
5. Keep one authoritative schema, constant, registry, policy, or normalizer;
   preserve old paths with re-exports rather than copies.
6. Keep provider-specific behavior in adapters; collection and health consume
   shared contracts.
7. Keep these facades thin: `watcher.run`, `watcher.source_health`,
   `watcher.health_alerts`, `watcher.sources.base`, `watcher.sources`, and
   `watcher.config`.
8. Preserve public/semi-public APIs and intentional test/script seams during
   moves unless the task explicitly allows breakage.
9. Split modules by semantic ownership when responsibilities diverge, not just
   because a file is long.
10. Refactor only with concrete evidence: duplicate logic, wrong dependency
    direction, measured import/context cost, repeated bugs, or a feature need.
11. Structural refactors preserve behavior, prove parity with focused tests,
    and remain separate from unrelated changes.
12. Add or extend a focused architectural guard test for dependency/ownership
    rules important enough to preserve.
13. Fit new code into the current architecture; parallel frameworks and second
    sources of truth are exceptional.

## Canonical routing

| Change | Owner |
| --- | --- |
| shared watcher/backend primitive | `internship_signal/domain/` |
| config model | `watcher/config/models.py` |
| environment parsing | `watcher/config/env.py` |
| config loading | `watcher/config/loader.py` |
| config validation | `watcher/config/validation.py` |
| source contract/error | `watcher/sources/contracts.py` |
| HTTP source plumbing | `watcher/sources/transport.py` |
| shared source parsing | `watcher/sources/parsing.py` |
| direct adapter registration | `watcher/sources/registry.py` |
| provider-specific behavior | provider adapter module |
| collection execution | `watcher/collection.py` |
| run orchestration | `watcher/pipeline.py` |
| run logging/timing | `watcher/run_logging.py` |
| health state/transition | `watcher/health/state.py` |
| health policy | `watcher/health/policy.py` |
| health persistence | `watcher/health/store.py` |
| health rendering | `watcher/health/rendering.py` |

For implementation detail, use [`docs/watcher.md`](docs/watcher.md),
[`docs/watcher-sources.md`](docs/watcher-sources.md),
[`docs/architecture.md`](docs/architecture.md),
[`docs/operations.md`](docs/operations.md), and
[`docs/testing.md`](docs/testing.md). Hosted behavior lives in the backend and
frontend hosted guides; scoring benchmark rules live in
[`evaluation/README.md`](evaluation/README.md).

## Tests

Run the narrowest suite covering the change; run the full backend + watcher suite
when touching `analyze_rows`, any shared `watcher/sources/` layer module,
`watcher/config/`, posting identity, the seen store, eligibility, or scoring.

```bash
PYTHONPATH=.:backend backend/venv/Scripts/python.exe -m pytest backend/tests watcher/tests -q
PYTHONPATH=.:backend python3 -m compileall -q internship_signal backend watcher scripts
cd frontend && npm test && npm run build
git diff --check
```

WSL fallback commands and benchmark rules: [`docs/testing.md`](docs/testing.md).

## Historical material

`docs/history/` holds the completed watcher implementation log. **Do not read it
to learn how the system works** — the docs above supersede it. Open it only when
you need historical context (why a decision was made, when a behavior was
introduced). Where it disagrees with current code, the code wins.
