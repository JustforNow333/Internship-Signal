# Architecture

Current component boundaries and data flow. Watcher-specific contracts live in
[`watcher.md`](watcher.md); operating the deployed system lives in
[`operations.md`](operations.md).

---

## Component map

```
internship-signal/
├── backend/
│   ├── app/
│   │   ├── main.py        FastAPI routes (ingest, jobs, summary, ask, profile)
│   │   ├── ingest.py      pipeline orchestration: process_csv + analyze_rows
│   │   ├── normalize.py   header mapping, cell cleaning, inference, dates
│   │   ├── dedupe.py      canonical keys, URL normalization, merge report
│   │   ├── salary.py      compensation parser -> USD/hr + confidence + notes
│   │   ├── classify.py    layered company classifier + role classifier
│   │   ├── signals.py     red flags, positive signals, profile match
│   │   ├── scoring.py     weighted categories + hard rules + actions
│   │   ├── eligibility.py backend-side eligibility helpers
│   │   ├── ask.py         interpret() / run_plan() + LLM integration point
│   │   ├── profile.py     student profile (data/profile.json overridable)
│   │   ├── config.py      weights, thresholds, FX table, paths
│   │   ├── store.py       in-memory dataset store
│   │   └── hosted/        multi-user accounts and jobs; matching.py (pure rules),
│   │                       match_service.py (reconciliation), notification_*.py
│   └── tests/             backend unit + hosted tests
├── watcher/               scheduled collector (see docs/watcher.md)
│   ├── run.py             entry point: python -m watcher.run (compatibility exports)
│   ├── pipeline.py        run_once stage orchestration and RunResult
│   ├── collection.py      adapter resolution, fetch outcomes, source attempts
│   ├── reporting.py       console report, heartbeat, health JSON report
│   ├── cli.py             argument parsing and process startup
│   ├── config.py          watchlist + env validation
│   ├── sources/           one module per ATS/backstop adapter
│   ├── eligibility.py     watcher-side degree/location/target-role gate
│   ├── filters.py         internship/open/min-score filtering
│   ├── seen_store.py      durable notification memory (seen.sqlite)
│   ├── analysis_cache.py  rebuildable static-analysis cache
│   ├── health/            models · sanitize · state · coverage · store
│   │                       policy · rendering · service · report
│   ├── source_health.py   watcher.health facade + health CLI entry point
│   ├── health_alerts.py   watcher.health alert facade
│   ├── source_comparison.py + audit_trace.py   bounded observability
│   ├── collection_snapshot.py / collection_concurrency.py
│   ├── notify.py          email digest rendering and send
│   ├── alumni.py          alumni roster load and company matching
│   └── tests/             offline adapter/pipeline tests with fixtures
├── frontend/
│   └── src/
│       ├── App.jsx        tabs: Overview / Postings / Buckets / Ask
│       ├── components/    table, drawer, dashboard, board, ask, upload
│       ├── hosted/        hosted multi-user UI
│       ├── utils/         pure filtering, sorting, formatting, CSV export
│       ├── hooks/         localStorage shortlist
│       └── __tests__/     vitest suites
├── scripts/               probes, benchmarks, canaries, migrations
├── evaluation/            scoring-benchmark tooling and docs
└── data/                  sample CSV, known-companies list, profile
```

---

## The `analyze_rows` seam

Everything after "rows exist" is one shared pipeline. A row is a dict keyed by
`CANONICAL_COLUMNS` (defined in `normalize.py`):

```python
CANONICAL_COLUMNS = [
    "company", "title", "location", "compensation", "description",
    "requirements", "source_url", "date_posted", "deadline",
    "remote_status", "internship_type",
]
```

- `backend/app/ingest.py::process_csv` owns CSV parsing, cleaning, and the
  cleaning report, then calls `analyze_rows`.
- `backend/app/ingest.py::analyze_rows(rows, today=None)` owns dedupe, salary
  parsing, classification, signals, scoring, IDs, and ordering for already
  canonical rows.

The watcher's only genuinely new work is producing canonical rows from source
adapters; it then reuses `analyze_rows` unchanged. **Watcher code must never
compute its own scores, role tracks, or job IDs**, and must never treat the
analyzed content-hash `job["id"]` as an ATS requisition ID.

---

## Data flow

**CSV / UI path**

```
CSV -> normalize -> dedupe -> per row (parse comp -> classify role ->
classify company -> flags/signals -> score) -> summary -> in-memory store
```

The dataset is stored in memory under a short id; the frontend keeps the full
scored array and filters/sorts client-side. Job ids are stable content hashes
(`sha1(company|title|location)[:10]`), so the localStorage shortlist survives
re-ingesting the same file.

**Watcher path**

```
collect (direct ATS, then GitHub backstops) -> deduplicate -> fingerprint ->
static-analysis cache lookup -> analyze misses -> score every row ->
assemble current jobs -> sort -> filter -> seen partition -> alumni join ->
digest email -> health/comparison persistence
```

Direct sources are fetched before GitHub so dedupe keeps direct provenance.
See [`watcher.md`](watcher.md) for the full contract.

**Hosted path**

Hosted multi-user storage, import, per-user matching, and notification delivery
live in `backend/app/hosted/` and PostgreSQL. Classification stays authoritative
in the watcher: hosted import gates on the watcher internship/co-op predicate
before role mapping. See [`../backend/HOSTED_BACKEND.md`](../backend/HOSTED_BACKEND.md)
and [`../frontend/HOSTED_API.md`](../frontend/HOSTED_API.md).

---

## Company classification is layered, not name matching

1. **Known lists** (`data/known_companies.json`, editable) — highest trust. An
   invalid optional `KNOWN_COMPANIES_PATH` falls back to built-ins; a key
   holding any non-string entry is invalid outright and never yields a
   fabricated company.
2. **Name tokens** — "Technologies", "Labs", "…AI", ".ai", "Robotics".
3. **Posting context** — 3+ technical-stack terms ⇒ tech; startup language
   ("seed-funded", "Series A", "8-person team") ⇒ startup; bakery/retail/
   staffing terms ⇒ non-tech.
4. **Role guard** — a clearly technical role title prevents a non-tech verdict
   from weak name evidence alone; the company stays `unknown — kept for review`.

Every verdict carries `confidence` and `evidence[]`, both surfaced in the UI.

---

## Scoring model

`score = Σ (category_score × weight)`, then hard rules. Weights live in
`backend/app/config.py` and sum to 1.00.

| Category | Weight | What it measures |
|---|---|---|
| role_relevance | 0.22 | Role type × your profile's role affinities |
| compensation | 0.16 | USD/hr band; unpaid=0, equity-only≈5 |
| legitimacy | 0.16 | Starts at 70; −30 per critical, −12 per major, −4 per minor flag; +12 reputable |
| learning_value | 0.14 | Mentorship, ownership, structured program, conversion |
| technical_depth | 0.12 | Concrete tools named; capped low for non-technical roles |
| effort_vs_value | 0.08 | Application hoops vs. what you get |
| location_convenience | 0.06 | Remote or near your preferred locations |
| deadline_urgency | 0.06 | Time pressure; expired = 0 |

Hard rules applied after the weighted sum:

- Any **critical** flag (e.g. asks applicants to pay) ⇒ capped at 40, bucket
  `low`, action `skip`.
- **Three or more major flags** ⇒ capped at 44, `low`, `skip`.
- **Expired deadline** ⇒ action `skip` regardless of score.
- Score ≥ 70 with no major flags ⇒ `apply_now`; ≥ 60 with a deadline inside
  7 days ⇒ `apply_now`; ≥ 55 ⇒ `apply_later`; ≥ 45 ⇒ `research_more`.

Buckets: **high ≥ 70**, **maybe 45–69**, **low < 45**. Every category returns a
one-line explanation, so any score can be audited from the UI drawer.

Only the watcher applies additional gates (student status, U.S. location,
target role). A watcher-ineligible job keeps its backend role/track but gets
`watcher_eligible=false`, `fit_score=0`, and action `skip`.

---

## Deterministic "ask the dataset"

`backend/app/ask.py` splits the feature in two:

- `interpret(question) -> QueryPlan` — keyword/regex rules producing a small,
  inspectable plan: `{intent, role, paid_only, remote_only, keywords}`.
- `run_plan(plan, jobs) -> answer` — pure filtering/ranking over already-scored
  jobs.

Every answer echoes its interpretation and applied filters and carries
`llm_note: "Answered by deterministic rules — no LLM involved."` Backend-specific
queries require a backend-adjacent `role_track` or the existing `backend_focus`
signal.

**LLM integration point:** replace only `interpret()` (marked
`# === LLM INTEGRATION POINT ===`, with an `ask_with_llm()` stub). `run_plan`
stays deterministic so answers remain grounded in actual rows.

---

## Backend request safety

- All JSON endpoints go through `backend/app/main.py::_json_object`. Malformed
  or excessively nested JSON, non-object bodies, and non-string
  `csv_text`/`question` values are HTTP 400, never internal errors.
- Multipart `file` must be an upload. CSV input is limited to 10 MiB; oversized
  input is HTTP 413.
- The frontend keeps CSV formula-injection protection on export.
- All text I/O is explicitly UTF-8.
