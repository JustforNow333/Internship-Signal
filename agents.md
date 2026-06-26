# Agents Guide

Before watcher-related work, read `WATCHER_SPEC.md` in the repo root in full.
It is the source of truth for the internship watcher plan.

Current foundational seam:

- Keep CSV reading and cleaning in `backend/app/ingest.py::process_csv`.
- Keep canonical-row analysis in `backend/app/ingest.py::analyze_rows`.
- `analyze_rows(rows, today=None)` accepts already-built canonical-shaped row
  dicts and returns the scored job dicts produced by the existing engine.
- Do not reimplement scoring, classification, salary parsing, dedupe, ids, or
  signal detection in watcher code. Reuse the existing backend functions.

Current watcher fetch layer:

- Source adapters live under `watcher/sources/`.
- `watcher/sources/base.py` owns the `Source` protocol, source errors,
  fetch helpers, and `make_row`.
- `watcher/config.py` owns `CompanyCfg`, `WatcherConfig`, and the small
  `watchlist.yml` loader. The loader intentionally supports the simple
  top-level `defaults` + `companies` YAML shape used by this repo; there is no
  PyYAML dependency.
- Implemented adapters: Greenhouse, Lever, and SimplifyJobs GitHub listings.
- Adapters return canonical-shaped rows plus `extra.source` (`direct` or
  `github`) and `extra.source_adapter`.
- Adapter tests parse saved fixtures only; tests must never hit the network.
- For future adapter work, verify the live endpoint first, save representative
  real responses under `watcher/tests/fixtures/`, and parse fixtures in tests.

Current watcher run core:

- `watcher/run.py` is runnable with `python -m watcher.run`.
- The default tiny live watchlist is `watcher/watchlist.yml` with Astera Labs
  (Greenhouse), Institute of Foundation Models (Lever), and GitHub
  (`github_only`).
- The run loop fetches direct rows first, then the GitHub backstop. This order
  is intentional: backend dedupe keeps the first duplicate row's `extra`, so
  direct rows win the source tag without changing backend dedupe.
- The run loop calls `backend.app.ingest.analyze_rows`; watcher code must not
  compute scores or ids itself.
- `watcher/filters.py` filters after scoring using existing job fields:
  target role defaults to `swe`, internships/co-ops only, open/non-expired only,
  optional `min_score` default off.
- `watcher/seen_store.py` is the SQLite seen-store. It keys on the existing
  analyzed job `id`, which comes from `backend.app.dedupe.job_id`. A job seen
  via GitHub is not new later via direct, and vice versa.
- Local seen-store files are ignored by `.gitignore`; pass `--seen-db` in tests
  or manual runs when you want an isolated store.

Scope guardrails:

- Do not add email, scheduling, alumni joins, GitHub Actions, or extra adapters
  unless the task explicitly asks for those steps.
- Preserve `process_csv` public behavior: return keys, job shape, cleaning
  report, summary, scoring, and ordering must remain compatible.
- Keep secrets out of the repo.
- Keep all text file reads/writes explicit about UTF-8.

Validation:

```bash
cd backend
python3 -m pytest tests/ -q
```

If the local environment uses the checked-in Windows virtualenv from WSL, run:

```bash
cd backend
venv/Scripts/python.exe -m pytest tests/ -q
```

To run backend tests plus watcher tests from the repo root:

```bash
PYTHONPATH=.:backend backend/venv/Scripts/python.exe -m pytest backend/tests watcher/tests -q
```

To run the watcher once from the repo root:

```bash
PYTHONPATH=.:backend python3 -m watcher.run --seen-db /tmp/internship_signal_watcher.sqlite
```
