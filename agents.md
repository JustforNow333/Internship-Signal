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
- `watcher/config.py::CompanyCfg` is the minimal per-company config object for
  the current adapter layer. Full `watchlist.yml` loading is not built yet.
- Implemented adapters: Greenhouse, Lever, and SimplifyJobs GitHub listings.
- Adapters return canonical-shaped rows plus `extra.source` (`direct` or
  `github`) and `extra.source_adapter`.
- Adapter tests parse saved fixtures only; tests must never hit the network.
- For future adapter work, verify the live endpoint first, save representative
  real responses under `watcher/tests/fixtures/`, and parse fixtures in tests.

Scope guardrails:

- Do not add more watcher subsystems, email, scheduling, persistence, filters,
  alumni joins, a run loop, or extra adapters unless the task explicitly asks
  for those steps.
- Preserve `process_csv` public behavior: return keys, job shape, cleaning
  report, summary, scoring, and ordering must remain compatible.
- Keep secrets out of the repo.

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
