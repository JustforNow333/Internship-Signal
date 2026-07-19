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
- The July 2026 season rollover is configuration-driven. Production targets
  `Summer 2027`, structured GitHub feed URLs are explicit/multi-feed capable,
  and season/feed health is visible in reports, digests, and heartbeats.
- Persistent per-company source health records direct and GitHub outcomes,
  transitions, recoveries, and effective coverage in the existing seen-store
  database without changing digest or seen semantics.
- The Actions final heartbeat now forwards the complete application heartbeat
  before appending seen-store persistence, and Workday isolates malformed
  posting records without hiding structurally broken/all-malformed feeds.
- Workday transport now has safe non-JSON diagnostics, bounded transient
  retries, configurable cross-tenant pacing, shared-incident reporting, and an
  isolated five-tenant local/Actions comparison probe.

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
   - Workflow dispatch input `send_email=false` is the priming mode: it exports
     `WATCHER_SEND_EMAIL=0`, suppresses the digest body so private alumni details
     do not enter Actions logs, marks new matches seen via
     `--mark-seen-without-send`, and saves the DB. This prevents the first later
     send from emailing the whole backlog.
   - Scheduled runs read the repository Actions variable `WATCHER_SEND_EMAIL`;
     live sends require repository secrets `SMTP_USER`, `SMTP_APP_PASSWORD`, and
     `EMAIL_TO`.
   - The workflow uses concurrency group `watcher-seen-store` with
     `cancel-in-progress: false` to serialize data-branch writes.
   - The app prints one heartbeat containing run, season, feed, source-health,
     alumni, send, and seen-marking fields. The workflow forwards that exact
     line and appends only `seen_loaded`, `seen_saved`, and `seen_store`.
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
6. Internship-season rollover:
   - `CompanyCfg` and `WatcherConfig` no longer insert a recruiting term when
     manually instantiated. `load_watchlist()` requires nonblank explicit
     `defaults.terms`, preserves inheritance, and rejects empty company
     overrides.
   - `defaults.github_listing_urls` accepts multiple validated HTTP/HTTPS
     endpoints. `GitHubListingsSource(url)` has no class-level production URL,
     and GitHub rows retain query-free `extra.feed_url` provenance.
   - Each configured feed is fetched once. Successful feeds aggregate, failed
     feeds remain visible and isolated, direct rows stay first, and backend
     analysis remains responsible for overlap deduplication.
   - `watcher/season.py` reports `ok`, `rollover_due`, `stale`, or `unknown`
     from explicit term years and identifies stale company overrides. Warnings
     never stop direct ATS coverage.
   - Reports, digest headers, application heartbeats, and workflow final
     heartbeats expose active terms, season status, and configured/successful
     feed counts. Heartbeat terms are comma-safe.
   - Production now targets `Summer 2027` with one live-verified official feed:
     `https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json`.
     SimplifyJobs has no separate official Summer 2027 repository as of July
     15, 2026, but the active feed contains the exact `Summer 2027` term.
   - Direct feed probe: HTTP 200, 11,147,156 bytes, top-level list with 14,973
     rows, and all 14,973 rows matched the expected required-key/list-field
     schema. The payload contained 269 `Summer 2027` rows.
   - Safe full dry probe used `/tmp/internship_signal_season_probe.sqlite`,
     forced email off, omitted `--mark-seen-without-send`, and completed with:
     `HEARTBEAT: ran, rows_fetched=17069, jobs_scored=15897, matches=68, new=68, errors=0, season_status=ok, configured_terms=Summer_2027, github_feeds_configured=1, github_feeds_succeeded=1, alumni_csv_status=loaded-csv, alumni_records_loaded=306, alumni_employers_indexed=278, sent=no, seen_marked=0`.
     The isolated DB contained zero seen rows afterward.
7. Persistent source-health monitoring:
   - `watcher/source_health.py` owns explicit attempt/state/transition/coverage
     models, pure status rules, stable query-free health keys, bounded error
     sanitization, SQLite persistence, aggregate summaries, JSON reports, and
     GitHub Actions summary rendering.
   - Every configured company records one direct outcome per run. Supported
     successes retain exact row counts, valid zero results stay successes,
     typed failures remain nonfatal, and `bespoke`/`github_only` entries record
     `unsupported` without advancing failure counters.
   - Every configured GitHub feed records an independent attempt. Valid payloads
     are healthy even with zero matching rows, and partial failures do not
     suppress successful feed/direct rows.
   - Direct sources are healthy on nonzero success, empty on initial/isolated
     zero success, degraded on one/two failures or repeated zero results after
     prior productivity, and failing after three failures. GitHub feeds do not
     use the zero-row degradation rule.
   - Status counters persist across runs in `source_health_attempts` and
     `source_health_current` inside the same `seen.sqlite`. Legacy seen-only
     databases upgrade automatically with their `seen` rows unchanged.
   - Coverage distinguishes operational zero-row sources from failed or
     unsupported direct sources, and counts a successful configured GitHub feed
     as an available backstop without requiring an active posting.
   - Logs, the normal report, application/final heartbeats, Actions annotations,
     and `$GITHUB_STEP_SUMMARY` expose health. The sanitized JSON handoff is
     configured with `WATCHER_HEALTH_REPORT_PATH` or `--health-report`.
   - Workflow database validation checks the existing `seen` table, both health
     tables, SQLite readability, nondecreasing seen count, and current-run
     health attempts before persisting the unchanged database path.
   - Source-health alerts remain GitHub Actions-only. There is no health-warning
     email, and health state does not mark jobs seen.
   - Safe isolated live verification used `WATCHER_SEND_EMAIL=0`, an empty
     injected alumni JSON map, a fresh `/tmp` SQLite database, no priming flag,
     and a `/tmp` JSON report. It completed with 129 configured companies, 59
     direct attempts/successes, 1 direct zero-row success, 0 direct failures,
     70 unsupported direct entries, 1/1 successful GitHub feed, coverage of 58
     `direct_covered`, 1 `direct_empty_but_responding`, and 70 `backstop_only`,
     with 0 uncovered companies. The database held 130 health attempt/current
     rows and 0 seen rows; the heartbeat reported `sent=no, seen_marked=0`.
8. Heartbeat forwarding and Workday record isolation:
   - The run step captures the last exact one-line application `HEARTBEAT:` and
     keeps all existing individual parsed outputs. The final step forwards that
     line and appends only `seen_loaded`, `seen_saved`, and `seen_store`, so new
     application fields need no workflow-template edit. Missing heartbeat data
     is surfaced as an error instead of a fabricated success.
   - Workday retains valid records from mixed pages, advances offsets by raw
     record count, and emits one bounded aggregate warning with stable skip
     reasons. Page-level invalid shapes and nonempty all-malformed fetches still
     raise `SourceSchemaError`; genuinely empty boards return `[]` successfully.
   - Existing source-health rows are not reset. A persisted degraded Merck row
     will transition naturally to `healthy` and report a recovery when a later
     partial fetch returns valid rows.
   - Safe Merck-only live verification explicitly set `WATCHER_SEND_EMAIL=0`
     and called only the configured Workday adapter. The current endpoint
     returned 943 raw postings, retained all 943 with usable titles/source URLs,
     and contained 0 malformed records (`skip_reasons=none`). No digest,
     seen-store, health database, alumni data, or temporary probe file was used.
9. Reliability audit:
   - Actions priming explicitly exports `WATCHER_SEND_EMAIL=0`; process-level
     false values win over dotenv. Its digest body is suppressed so private
     alumni annotations do not enter workflow logs.
   - Seen-store batch writes are transactional. Mixed-validity batches roll
     back completely instead of leaving a partially marked digest.
   - All reusable direct adapters retain valid records from mixed malformed
     payloads and reject nonempty all-malformed payloads. SmartRecruiters and
     Workday detect repeated pages instead of looping; Workday diagnostics reset
     between company fetches.
   - Watchlist loading rejects normalized company/alias collisions and GitHub
     URLs whose query-only differences would share one persisted health key.
   - Backend CSV ingestion has a 10 MiB limit and missing sample responses no
     longer expose server paths. CSV export neutralizes spreadsheet formulas in
     untrusted text fields. Live SMTP logs no longer include the recipient.
10. Shared Workday transport reliability:
   - Non-JSON failures now carry stable structured classifications and safe
     metadata (status, query-free URL, content type/encoding, bounded size,
     generic body kind, SHA-256 digest, and attempt count). Raw bodies, cookies,
     sensitive headers, and challenge values are never logged or persisted.
   - Workday retries only transient network, 429, selected 5xx, empty, and
     transient HTML/non-JSON failures, with three total attempts and bounded
     injectable backoff. Deterministic schema failures still fail immediately.
   - An instance-local pacer defaults to 0.5 seconds between starting different
     tenants and is configurable with `WATCHER_WORKDAY_MIN_INTERVAL_SECONDS`;
     pagination pages are not tenant-paced.
   - A shared incident is reported when at least five Workday tenants fail and
     one supported transient classification accounts for at least 60% of those
     failures. Per-company attempts and health counters remain unchanged.
   - A safe local five-tenant probe on July 19, 2026 returned valid JSON on the
     first attempt for Cornerstone Research, Merck, Capital One, Salesforce,
     and Eli Lilly and Company across `wd501`, `wd5`, `wd12`, and `wd115`.
     This disproves 24 simultaneous tenant configuration errors locally but
     cannot distinguish a time-limited incident from GitHub-runner-specific
     blocking until the isolated manual Actions probe is deployed and run.

## Next

- Run the first manual GitHub Actions priming dispatch with `send_email=false`.
- After confirming the data branch exists and the heartbeat looks right, set the
  repo Actions variable `WATCHER_SEND_EMAIL=true` to enable scheduled sends.
- A future, separately scoped enhancement may add a dedicated source-health
  email policy; it must not be coupled to internship-match digest conditions.

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

Latest local validation after the reliability audit:

```text
354 passed, 1 warning in 2.69s
```
