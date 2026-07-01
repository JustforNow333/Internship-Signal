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
- The config schema accepts the §3 ATS values
  (`greenhouse`, `lever`, `ashby`, `smartrecruiters`, `workable`, `workday`,
  `bespoke`, `github_only`) plus generated metadata fields like
  `workday_shard`, `workday_site`, `module`, `alumni_match`, `source_url`,
  and `note`. Workday entries require tenant `token`, `workday_shard`
  (example: `wd12`), and `workday_site`.
- Implemented adapters: Greenhouse, Lever, Ashby, SmartRecruiters, Workable,
  Workday, and SimplifyJobs GitHub listings.
- Adapters return canonical-shaped rows plus `extra.source` (`direct` or
  `github`) and `extra.source_adapter`.
- Adapter tests parse saved fixtures only; tests must never hit the network.
- For future adapter work, verify the live endpoint first, save representative
  real responses under `watcher/tests/fixtures/`, and parse fixtures in tests.
- Workday uses POST
  `https://{tenant}.{workday_shard}.myworkdayjobs.com/wday/cxs/{tenant}/{workday_site}/jobs`
  with JSON pagination. Capital One rejected limits above 20, so the adapter
  uses `limit: 20`. Some Workday URLs from the generated watchlist currently
  return Workday maintenance/refresh HTML instead of JSON; the adapter treats
  that as a fetch failure rather than a silent empty result.
- Workable uses the current public careers API
  `POST https://apply.workable.com/api/v3/accounts/{token}/jobs`. ICEYE's
  live board currently reports zero openings.
- `watcher/detect.py` is a self-contained convenience tool, runnable as
  `python -m watcher.detect "Company Name"`. It may hit the network, but it is
  not part of the scheduled run path and must never fabricate ATS tokens. The
  generated priority-company research report lives at
  `watcher/detect_report.md`.
- The detector should only resolve Workday when it has tenant, shard, and site;
  report output should show Workday as `tenant/shard/site`.

Current watcher run core:

- `watcher/run.py` is runnable with `python -m watcher.run`.
- The default `watcher/watchlist.yml` is now the generated starter priority
  watchlist. It contains resolved direct ATS entries, `bespoke` entries for
  non-standard portals, and `github_only` entries for unresolved companies.
- The run loop skips `bespoke` and `github_only` entries for direct fetching,
  fetches direct rows first, then the GitHub backstop. This order is
  intentional: backend dedupe keeps the first duplicate row's `extra`, so
  direct rows win the source tag without changing backend dedupe.
- The run loop calls `backend.app.ingest.analyze_rows`; watcher code must not
  compute scores or ids itself.
- `watcher/filters.py` filters after scoring using existing job fields:
  target role defaults to `swe`, internships/co-ops only, open/non-expired only,
  optional `min_score` default off.
- `watcher/seen_store.py` is the SQLite seen-store. It keys on the existing
  analyzed job `id`, which comes from `backend.app.dedupe.job_id`. A job seen
  via GitHub is not new later via direct, and vice versa.
- `watcher/alumni.py` loads the private gitignored `watcher/alumni.csv`,
  indexes alumni by `backend.app.dedupe.norm_company(Employer)`, and annotates
  matches with `alumni` after filtering. Alumni data is additive only; it must
  never drop, reorder, gate, or rescore a posting.
- `watcher/notify.py` renders one plain-text email digest for genuinely new
  matches. `render_digest(matches)` is pure and offline-tested. `send_digest`
  dry-runs to stdout unless `WATCHER_SEND_EMAIL` is truthy; live Gmail SMTP
  requires `SMTP_USER`, `SMTP_APP_PASSWORD`, and `EMAIL_TO` from env.
- The run loop marks jobs seen only after `send_digest` reports a successful
  live send by default. Dry-run digest previews do not advance the seen-store
  unless `python -m watcher.run --mark-seen-without-send` is used for the
  explicit GitHub Actions priming flow.
- Digest decisions are settled: no score gate, sort by score descending with
  company/title tie-breaks, and send nothing when there are zero new matches.
- Local seen-store files are ignored by `.gitignore`; pass `--seen-db` in tests
  or manual runs when you want an isolated store. The default seen-store path is
  `watcher/seen.sqlite`, configurable with `WATCHER_SEEN_DB`.
- `.github/workflows/watcher.yml` runs the watcher hourly and by manual
  dispatch. It restores `seen.sqlite` from the orphan `watcher-data` branch into
  the path named by `WATCHER_SEEN_DB`, runs `python -m watcher.run`, then commits
  and pushes the DB back to `watcher-data`.
- Workflow dispatch input `send_email=false` is the priming path: it unsets
  `WATCHER_SEND_EMAIL`, prints a dry-run digest, marks the new matches seen with
  `--mark-seen-without-send`, and persists that DB so the first later send does
  not email the whole backlog. Scheduled runs read the repo Actions variable
  `WATCHER_SEND_EMAIL`; live sends require the repo secrets `SMTP_USER`,
  `SMTP_APP_PASSWORD`, and `EMAIL_TO`.
- The watcher prints a run heartbeat, and the workflow prints a final heartbeat
  including run counts, source-error count, seen-store load/save counts, send
  result, and persistence status. Source adapter failures are logged and
  surfaced as warnings; seen-store load corruption or push failure is a hard
  workflow failure.

Scope guardrails:

- Do not add scheduling, GitHub Actions, or extra adapters unless the task
  explicitly asks for those steps.
- Do not change scoring, classification, salary parsing, filters, the
  seen-store, source adapters, source dispatch, alumni matching, or digest
  decisions unless the task explicitly asks for that exact layer.
- Preserve `process_csv` public behavior: return keys, job shape, cleaning
  report, summary, scoring, and ordering must remain compatible.
- Keep secrets out of the repo.
- Keep all text file reads/writes explicit about UTF-8.
- Keep watcher progress notes in the root `WATCHER_PROGRESS.md` only. Do not
  recreate stale duplicate handoff files under `watcher/`.

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

When launching the checked-in Windows virtualenv from WSL and inline env vars do
not cross into the Windows process, use the Windows shell to set `PYTHONPATH`:

```bash
cmd.exe /C "cd /D C:\Users\burst\internship-signal && set PYTHONPATH=C:\Users\burst\internship-signal;C:\Users\burst\internship-signal\backend && backend\venv\Scripts\python.exe -m pytest backend\tests watcher\tests -q"
```

Also run a syntax pass after broad Python edits:

```bash
PYTHONPATH=.:backend python3 -m compileall -q backend watcher
```

For frontend changes:

```bash
cd frontend
npm test
npm run build
```

To run the watcher once from the repo root:

```bash
PYTHONPATH=.:backend python3 -m watcher.run --seen-db /tmp/internship_signal_watcher.sqlite
```
