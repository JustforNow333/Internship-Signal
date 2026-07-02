# Internship Watcher - progress & handoff

## Source Of Truth

`WATCHER_SPEC.md` in the repo root remains the authoritative build spec.
This file tracks completed watcher steps and the next handoff target.

## Current Status

- Backend `analyze_rows(rows, today=None)` seam is built and reused by watcher.
- Watcher source layer is built: Greenhouse, Lever, Ashby, SmartRecruiters,
  Workable, Workday, and SimplifyJobs GitHub listings.
- `watcher/detect.py` and the generated priority `watcher/watchlist.yml` are in
  place.
- `watcher/alumni.py` is built and real-data verified against the private
  gitignored `watcher/alumni.csv` roster. Alumni annotations are additive only.
- `watcher/notify.py` is built for the email digest. Dry-run is the default;
  live Gmail SMTP is opt-in via env.
- `.github/workflows/watcher.yml` is built for hourly/manual GitHub Actions
  runs with SQLite seen-store persistence on the orphan `watcher-data` branch.
- The July 2026 alumni-company watchlist expansion is built with 18 additional
  targets, using direct adapters only where live endpoints matched current
  source support and `bespoke` notes for unsupported or unsafe-to-scope portals.

## Done

1. `analyze_rows` refactor.
2. Source adapters, watcher core, detect helper, generated watchlist, and
   alumni join.
   - Real roster verification: `watcher/alumni.csv` is present with 332 data
     rows. It has the five required columns plus an ignorable extra
     `First and last name` column.
   - `load_alumni()` indexes 306 records across 278 employer keys. The 26-row
     gap is entirely blank `Employer` values; the loader drops no duplicate
     rows and found no other unindexable nonblank employers.
   - No rows have blank first/last-name fields while the combined
     `First and last name` field is populated.
   - Real watchlist fuzzy matches found no obvious false positives. The current
     logged fuzzy matches are Balyasny Asset Management, Chainalysis, MIT
     Lincoln Laboratory, and Northrop Grumman against roster spelling variants.
   - The roster typo `Capitol One` now attaches to `Capital One` postings via
     the alias tier. `Chainalysis` continues to attach to the roster typo
     `Chainanalysis` through the fuzzy tier.
3. Email digest:
   - `render_digest(matches)` is pure and offline-tested.
   - `send_digest(matches)` sends nothing for zero new matches.
   - Dry-run prints the rendered digest unless `WATCHER_SEND_EMAIL` is truthy.
   - Live send requires `SMTP_USER`, `SMTP_APP_PASSWORD`, and `EMAIL_TO`.
   - The seen-store advances only after a successful live send; dry-run digest
     previews do not mark postings seen.
   - Digest includes all new SWE-intern matches with no score gate, sorted by
     score descending.
   - Digest rows show score, recommendation, top reason, red flags, apply URL,
     source tag, and alumni annotations.
   - Scheduler handoff suite after later additions: `147 passed, 1 warning`.
4. Scheduler + seen-store persistence:
   - `.github/workflows/watcher.yml` runs hourly plus `workflow_dispatch`.
   - The watcher runs as `python -m watcher.run` with `PYTHONPATH=.:backend`,
     Python 3.11, and dependencies from `backend/requirements.txt`.
   - The workflow points the app at `.watcher-state/seen.sqlite` through
     `WATCHER_SEEN_DB`. The app default remains `watcher/seen.sqlite`, also
     configurable with `WATCHER_SEEN_DB` or `--seen-db`.
   - The persisted DB is committed as `seen.sqlite` on the orphan branch
     `watcher-data`, never on `main`.
   - Load logs either `SEEN-STORE: bootstrapping empty (no prior data branch)`
     or `SEEN-STORE: loaded N seen ids`. Corrupt persisted DBs fail the job
     during load.
   - Save commits and pushes back to `watcher-data`; push rejection triggers a
     bounded three-attempt fetch/reset/retry loop. Final push failure is a hard
     workflow failure.
   - Workflow dispatch input `send_email=false` is the priming mode: it unsets
     `WATCHER_SEND_EMAIL`, prints the dry-run digest, marks new matches seen via
     `--mark-seen-without-send`, and saves the DB. This prevents the first later
     send from emailing the whole backlog.
   - Scheduled runs read the repository Actions variable `WATCHER_SEND_EMAIL`;
     live sends require repository secrets `SMTP_USER`, `SMTP_APP_PASSWORD`, and
     `EMAIL_TO`.
   - The workflow uses concurrency group `watcher-seen-store` with
     `cancel-in-progress: false` to serialize data-branch writes.
   - The app prints a run heartbeat:
     `HEARTBEAT: ran, rows_fetched=..., jobs_scored=..., matches=..., new=..., errors=..., sent=..., seen_marked=...`.
   - The workflow prints a final heartbeat including seen-store state:
     `HEARTBEAT: ran, rows_fetched=..., jobs_scored=..., matches=..., new=..., errors=..., seen_loaded=..., seen_saved=..., sent=..., seen_store=...`.
   - Live validation by actual GitHub manual dispatch remains for the user to
     run.
5. Alumni-company watchlist expansion:
   - Added DoorDash, Tesla, ASML, HP, ZoomInfo, Intuitive Surgical, Whatnot,
     Augury, Goldman Sachs, JPMorgan Chase, Barclays, UBS, Nomura, BlackRock,
     AQR Capital, Federal Reserve Bank of New York, KPMG, and EY.
   - Verified direct endpoints for DoorDash, ASML, HP, ZoomInfo, Intuitive
     Surgical, Augury, BlackRock, and AQR Capital. Barclays' Workday board is
     reachable but fails the current Workday adapter schema, so it is marked
     `bespoke` until adapter follow-up is approved.
   - Unsupported custom, Oracle HCM, Taleo-style, SuccessFactors, and unsafe
     broad Workday portals are documented as `bespoke` rather than direct ATS
     entries.
   - Local throwaway dry-run completed with:
     `HEARTBEAT: ran, rows_fetched=14576, jobs_scored=13514, matches=69, new=69, errors=0, sent=no, seen_marked=0`.

## Next

- Run the first manual GitHub Actions priming dispatch with `send_email=false`.
- After confirming the data branch exists and the heartbeat looks right, set the
  repo Actions variable `WATCHER_SEND_EMAIL=true` to enable scheduled sends.

## Validation Command

```bash
PYTHONPATH=.:backend backend/venv/Scripts/python.exe -m pytest backend/tests watcher/tests -q
```

When launching the checked-in Windows venv from WSL, inline WSL env assignments
may not cross into the Windows process. The verified equivalent command from
WSL is:

```bash
cmd.exe /C "cd /D C:\Users\burst\internship-signal && set PYTHONPATH=C:\Users\burst\internship-signal;C:\Users\burst\internship-signal\backend && backend\venv\Scripts\python.exe -m pytest backend\tests watcher\tests -q"
```

Latest local validation after watchlist expansion:

```text
154 passed, 1 warning in 0.74s
```
