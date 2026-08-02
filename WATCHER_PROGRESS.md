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
- A deterministic real-posting scoring benchmark exporter/evaluator now
  supports blind human labeling and frozen baseline/current comparisons without
  changing scoring or watcher state.
- Typed GitHub backstops now include the independent `sndsh404` Summer 2027
  Markdown table after Simplify, with fixed source priority, merged provenance,
  and normalized-URL seen suppression.
- Production watcher eligibility now conservatively rejects explicit non-U.S.
  locations with `outside_us` while retaining U.S., multi-location U.S., and
  country-ambiguous roles without changing backend scores or role decisions.
- Categorical student eligibility now excludes only clear PhD-only,
  graduate-only, freshman-only, and returning-intern-only restrictions, with
  stable evidence-backed audit reasons and mixed/ambiguous cases retained.
- Source-comparison tracing now builds one run-wide posting-identity context
  instead of rescanning the full posting universe for every analyzed job.
- A separate frozen U.S. role-fit benchmark now preserves the historical
  location-gate benchmark while measuring role relevance only on production
  location statuses `us` and `ambiguous`.
- Watcher source and stage timing now uses monotonic high-resolution
  `perf_counter` measurements and sanitized machine-readable INFO logs without
  changing heartbeat, collection, scoring, notification, or persistence data.
- Backend analysis now shares one posting text/match context across
  classification, signals, profile matching, and scoring, while preserving
  context-free callers and serialized job output.
- Watcher runs now persist versioned static-analysis artifacts in the existing
  SQLite state and always rebuild current scores/final jobs from current rows;
  backend CSV/API analysis remains cache-independent.
- Live collection now produces one immutable, versioned `CollectionBatch`.
  Atomic gzip JSON snapshots can replay that batch through the unchanged
  post-collection pipeline without network or operational state side effects.

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
   - Workflow dispatch separates `send_email` from `prime_seen`. Email-disabled
     runs are side-effect-free for job-notification state unless priming is
     explicitly requested; priming stores `primed_at`, while successful live
     sends store `emailed_at`.
   - Scheduled runs read the repository Actions variable `WATCHER_SEND_EMAIL`;
     live sends require repository secrets `SMTP_USER`, `SMTP_APP_PASSWORD`, and
     `EMAIL_TO`. Recognized true/false values are explicit; missing/blank and
     invalid nonblank values remain distinguishable while resolving safely to
     false.
   - The workflow uses concurrency group `watcher-seen-store` with
     `cancel-in-progress: false` to serialize data-branch writes.
   - The app prints one heartbeat containing run, season, feed, source-health,
     alumni, send, and seen-marking fields. The workflow forwards that exact
     line, appends scheduled-delivery diagnostics, then appends `seen_loaded`,
     `seen_saved`, and `seen_store`.
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
     forced email off, omitted explicit priming, and completed with:
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
11. Real-posting scoring benchmark:
   - `scripts/build_scoring_benchmark.py` collects through the normal watcher
     adapters, analyzes through the existing backend seam, keeps all open
     internships regardless of watcher eligibility, and deterministically
     samples random, top-ranked, and difficult cohorts.
   - Exports are a blind formula-safe labels CSV, canonical frozen rows JSONL,
     baseline predictions keyed by stable job ID, and a manifest containing
     Git/source/count metadata plus deterministic hashes.
   - `scripts/evaluate_scoring_benchmark.py` is offline, validates complete or
     explicitly partial labels, rescores at the frozen date, requires exact ID
     joins, and reports random-cohort eligibility metrics, ranking metrics,
     score bands, error diagnostics, disagreements, and baseline/current
     changes in Markdown and JSON.
   - Generated real-posting benchmark sets live only in gitignored
     `evaluation/private/`. No alumni data, email, seen-store, `watcher-data`,
     workflow artifact, or hourly integration is involved.
12. Typed GitHub backstops and `sndsh404` Markdown source:
   - `defaults.github_listing_sources` supports named `simplify_json` and
     `github_markdown_table` entries; legacy `github_listing_urls` remains
     compatible. Production config now contains Simplify plus
     `sndsh404_summer_2027`, sorted by fixed format priority rather than YAML
     order.
   - The Markdown adapter finds the five-column table by headers, parses escaped
     text and Markdown apply links, isolates malformed rows with one bounded
     warning, rejects missing/invalid/all-malformed tables, applies exact
     watchlist/term filters, and retains source-only `Added` dates.
   - Closed, sponsorship, and citizenship markers are stored then removed from
     display fields. Lower-priority closure cannot override active direct or
     Simplify data.
   - Canonical dedupe uses direct ATS, Simplify, then Markdown precedence,
     records `primary_source`, ordered `sources`, and per-source details, and
     recognizes official Greenhouse host/`gh_jid` URL aliases. Seen suppression
     now also uses normalized stored URLs across runs.
   - Offline backend/watcher validation: `464 passed, 1 warning`; frontend:
     `23 passed`, production build succeeded. Compileall and diff checks passed.
   - Isolated live probe used email off, empty injected alumni JSON, a fresh
     `/tmp` SQLite database/report, and no priming flag. The final-code run
     fetched 17,540 rows, scored 16,283 jobs, found 65 new dry-run matches, had
     0 errors, recorded
     129 company plus 2 independent feed-health attempts, and left `seen` at
     zero. Simplify returned 4 watchlist rows and `sndsh404` returned 9; both
     were healthy. No normal workspace seen database appeared.
   - A narrow live Anduril verification found one posting in all three sources
     and produced one active canonical row with direct fields and provenance
     `direct_ats,simplify,sndsh404_summer_2027`.
   - `evaluation/README.md` documents commands, the label rubric, sampling
     interpretation, privacy, and later scoring-version comparison.
13. Production U.S.-location eligibility:
   - `watcher/eligibility.py::assess_us_location()` is the reusable tri-state
     helper. Explicit U.S. evidence wins across multiple locations, explicit
     foreign country/region evidence returns `outside_us`, and missing/vague
     locations remain eligible for normal role checks.
   - State abbreviations are never country evidence; foreign examples such as
     `Madrid, MD, Spain` and `Schiphol, NH, Netherlands` are rejected by their
     explicit country text.
   - The offline evaluator applies the production gate to current predictions
     while retaining original fit scores, actions, role tracks, and rankings.
     Export sampling still keeps all open internships, including international
     rows.
   - Frozen `scoring_20260724` reevaluation improved random-cohort false
     positives from 9 to 0, true negatives from 90 to 99, specificity from
     90.9% to 100%, and accuracy from 90% to 99%. Fit-score, role-track,
     action, degree, and ranking changes were all zero.
   - Across all 161 selected rows, six false positives remain: four labeled
     `unrelated_role` and two city-only `outside_us` labels (`Utrecht` and
     `Santiago`) intentionally retained as ambiguous. One U.S. ML research
     false negative remains. All 146 international-labeled rows remain in the
     frozen set; input hashes were unchanged and only report/metrics refreshed.
   - Offline backend/watcher validation: `486 passed, 1 warning`; compileall
     completed successfully.
14. U.S. role-fit benchmark construction:
   - `scripts/build_us_rolefit_benchmark.py` reuses normal collection,
     `analyze_rows()`, frozen-row/prediction helpers, and the offline evaluator.
     It excludes only `assess_us_location()==outside_us` before independently
     selecting `random`, `likely_match`, and `difficult_negative` cohorts.
   - The immutable historical `scoring_20260724_*` files remain separate. New
     artifacts use prefix
     `evaluation/private/scoring_us_rolefit_20260726`.
   - The prior provisional dirty-tree export is retained only as comparison
     metadata: 68 IDs, cohort counts 68 random / 16 likely-match / 56
     difficult-negative, and location statuses 54 `us` / 14 `ambiguous`.
   - A valid freeze requires committing the construction first, a clean tracked
     tree, `git_dirty=false`, the exact committed SHA, blank human fields, and
     verified labels/IDs/rows/predictions/watchlist hashes.
15. Structured U.S.-location evidence:
   - The shared production helper now prefers structured country values within
     each location, recognizes unambiguous ISO alpha-3 prefixes such as `NLD`,
     `CHE`, and `POL`, and uses bounded country evidence only from strong
     posting-location context when an ATS supplies a city alone.
   - Greenhouse, Lever, Ashby, SmartRecruiters, and Workable retain their
     already-available structured location/country metadata in canonical
     `extra`; classification and scoring remain backend-owned and unchanged.
   - The frozen 74-row U.S. role-fit reevaluation changed seven location
     statuses and four eligibility decisions, all human-labeled `outside_us`.
     False positives fell from 9 to 5; precision rose from 43.8% to 58.3% and
     F1 from 48.3% to 56.0%. Fit scores, roles, actions, degree decisions, and
     ranking values did not change.
   - Offline backend/watcher validation: `499 passed, 1 warning`; compileall
     completed successfully.
16. Frozen U.S. role-fit classifier refinement:
   - Title-first reusable rules recognize applied AI integration, technical
     digital solutions/workflow automation, quantitative analyst/trading,
     technical product/APM, and umbrella programs only with an explicit
     relevant technical track.
   - Strong naval/mechanical/physical-product, electrical-hardware, consumer
     research, and manufacturing-quality evidence now defeats incidental AI,
     software, Python, modeling, analytics, firmware, or testing mentions.
     Explicit firmware/embedded software and software QA automation remain.
   - On the frozen 74-row set, true positives rose from 7 to 12, false
     positives fell from 5 to 0, false negatives fell from 6 to 1, precision
     reached 100%, recall 92.3%, and F1 96.0%. The unsupported BlackRock
     all-tracks row remains excluded because its frozen fields contain no
     explicit technical track.
   - Offline backend/watcher validation: `515 passed, 1 warning`; compileall
     completed successfully.
17. Evidence-based parser and source robustness fixes:
   - The Go/Rust fit penalty no longer mistakes the ordinary lowercase verb
     `go` for the Go programming language; explicit `Go`, `GO`, `Golang`, and
     Rust evidence remains recognized.
   - Compensation parsing ignores unmarked program-duration counts such as
     `12-week internship` instead of widening an hourly pay range with them.
   - The Simplify/GitHub JSON backstop retains schema-valid entries from a
     mixed malformed payload, emits one bounded warning, and still fails a
     nonempty all-malformed payload.
   - Offline backend/watcher validation: `519 passed, 1 warning`; frontend:
     `23 passed`; production build and Python compileall completed successfully.
18. Independent U.S. holdout tooling:
   - `scripts/build_us_holdout_benchmark.py` adds clean expected-commit
     enforcement before collection and freeze, private-path enforcement,
     deterministic `random`/`likely_match`/`difficult_negative` cohorts, and
     complete-pool behavior for undersized candidate populations.
   - Prior labels are never opened. Validated prior rows/predictions/manifests
     provide stable-ID, normalized-URL, and normalized fallback exclusion keys;
     selected rows must have zero overlap under all three methods.
   - Manifests retain sanitized source/location provenance, collection
     failures, distributions, coverage limitations, configuration/input/output
     hashes, and explicit email/alumni/seen-state isolation.
   - This is the tooling stage only. Commit these tracked changes before a
     later clean run performs live collection and freezes holdout artifacts.
   - Offline backend/watcher validation: `533 passed, 1 warning`; compileall
     completed successfully.
19. Notification state and posting identity:
   - Live send, dry run, and explicit prime are separate modes. Dry runs do not
     insert or update `seen`; successful sends populate `emailed_at`; explicit
     priming populates `primed_at`; failed sends leave postings pending.
   - Legacy rows with blank `emailed_at` and no `primed_at` are pending rather
     than suppressed. SQLite migrates existing databases in place without
     deleting or rebuilding `seen`.
   - Collection dedupe and notification suppression share requisition-first,
     posting-URL-second, exact fallback identity. Generic/shared careers URLs
     cannot collapse distinct stable requisitions, and direct ATS provenance
     still wins genuine direct/GitHub duplicates.
   - Run reports expose eligible/new/emailed-suppressed/primed-suppressed/
     dry-pending counts plus cross-source merges and bounded suppressed labels.
   - Offline backend/watcher validation: `554 passed, 1 warning`; frontend:
     `23 passed`; production build and Python compileall completed successfully.
   - Isolated temporary-SQLite integration output:
     `eligible=6 dry_new=6 seen_after_dry=0 live_emailed=6 rerun_new=0
     primed_seventh=1 after_prime_new=0 cross_source_merged=1`.
20. Conservative categorical student eligibility:
   - `backend/app/eligibility.py` evaluates structured eligibility, title,
     required qualifications, and mandatory description evidence in fixed
     priority order. Preferred, incidental, mixed-group, and ambiguous
     evidence remains eligible.
   - Stable reasons are `phd_only`, `graduate_only`, `freshman_only`, and
     `returning_intern_only`. Excluded jobs keep their technical role/track and
     original fields while using the existing zero-fit/skip convention.
   - Watcher and benchmark audit output exposes the stable reason, bounded
     triggering evidence, and evidence source. Normal run reports list
     categorical exclusions without placing them into email or seen-state
     selection.
   - The Northrop Grumman `2027 Returning Intern Software Engineer` fixture is
     excluded from dry/live digests as `returning_intern_only`; a separate
     open requisition remains distinct and is the only row marked emailed.
   - Offline backend/watcher validation: `588 passed, 1 warning`.
21. Posting audit, source comparison, and independent health alerts:
   - `python -m watcher.audit` supports state-only and live read-only tracing by
     company/alias, title, URL, requisition, analyzed ID, or canonical identity.
     Structured console/JSON output exposes every pipeline stage, dedupe
     provenance, notification timestamps, and stable final reasons.
   - Sanitized source-comparison snapshots classify GitHub-only, direct-only,
     merged, rejected, and no-posting results. Aggregate history retains 30
     runs; posting details retain three.
   - Source-health mail has independent modes, rendering, SMTP invocation,
     cooldown/recovery/daily-summary tables, and heartbeat fields. It never
     writes internship `emailed_at` or `primed_at`.
   - Offline backend/watcher validation: `639 passed, 1 warning`; Python
     compileall completed successfully.
22. Evidence-backed repository audit:
   - ATS date normalization now treats non-finite and platform-out-of-range
     numeric timestamps as unknown instead of letting one malformed external
     value abort adapter parsing.
   - Source-comparison traces recursively sanitize arbitrary persisted text and
     strip credentials/query data from URL identities while leaving production
     posting identity and dedupe decisions unchanged.
   - A recovery alert whose SMTP delivery fails remains pending and is retried
     once on a later healthy run; a successful retry restores normal
     suppression.
   - `watcher.audit` opens an in-memory migrated snapshot of seen state and a
     read-only comparison connection. State-only audit no longer creates a
     missing SQLite file or migrates an existing database on disk.
   - Removed one unreferenced private renderer, unused daily-summary inputs,
     duplicated alert sorting, and verified unused imports. A benchmark
     module-level re-export that appeared unused was retained after its public
     test dependency was confirmed.
   - Offline backend/watcher validation: `647 passed, 1 warning`; frontend:
     `23 passed`; production build and Python compileall completed successfully.
23. Actions rollout performance regression:
   - Safe manual run `30418538731` confirmed
     `send_email=false`, `prime_seen=false`, `health_email_mode=off`, and 99
     loaded seen rows, then reached dry-run notification selection without
     changing notification state.
   - The run exposed quadratic source-comparison work: after analyzing 17,566
     rows and filtering 17,533 jobs, per-posting whole-universe identity and
     similar-requisition scans prevented the application heartbeat from
     completing. The obsolete run was canceled before persistence.
   - Audit tracing now precomputes non-specific URLs, notification records, and
     similar requisitions once per run. A scale regression asserts one universe
     scan and no per-posting fallback scans while preserving bounded similar
     requisition diagnostics.
   - Corrected safe run `30419701605` completed in 9m20s with an exact dry-run
     heartbeat and unchanged notification timestamps, then exposed a separate
     state-size bound: one 17,600-entry comparison snapshot grew SQLite from
     5.7 MB to 86.9 MB and triggered GitHub's large-file warning.
   - Persisted comparison details now retain at most 1,000 category-balanced
     entries per run, retroactively cap retained legacy runs, and vacuum only
     when free pages are at least 25% of the database. Exact aggregate counts
     remain in the 30-run summaries.
   - A copy of the real oversized database compacted from 86.9 MB to 14.6 MB
     with two 1,000-detail runs, zero free pages, and `quick_check=ok`.
   - Offline backend/watcher validation: `649 passed, 1 warning`; focused
     audit/comparison/seen/run validation: `94 passed`.
24. Actions rollout eligibility audit:
   - Safe manual run `30420413509` completed on the bounded comparison
     implementation with 99 seen rows loaded/saved, `sent=no`,
     `seen_marked=0`, no health email, and a 14.6 MB validated state database.
   - Production traces exposed two conservative-classification gaps:
     `Advanced degree not required` was treated as mandatory, and a current
     bachelor's/current master's alternative was treated as graduate-only.
   - Negated degree requirements are now removed before mandatory-pattern
     matching, while explicit mixed current-student alternatives remain
     eligible. Genuine MS-or-higher requirements remain graduate-only.
   - Offline backend/watcher validation: `651 passed, 1 warning`; Python
     compileall completed successfully.
25. Eligibility-order and comparison-retention correction:
   - Normal watcher decisions now establish internship/co-op/student-program
     and open status before categorical student restrictions. Full-time senior
     and manager roles therefore resolve as `not_internship`; open internships
     retain the existing categorical outcomes.
   - Categorical traces preserve bounded evidence plus explicit mandatory,
     negation, and mixed-eligibility diagnostics. Degree keywords, preferred
     qualifications, negated requirements, and undergraduate/graduate
     alternatives do not establish graduate-only or PhD-only eligibility.
   - Comparison summaries retain exact aggregate counts for 30 runs. Detailed
     rows retain three runs, all eligible/no-posting/anomaly details, 25
     deterministic routine-rejection samples per reason, and at most 2,000
     total rows per run. JSON artifacts use the same policy.
   - Legacy detail cleanup is transactional and leaves notification,
     source-health, and alert tables untouched. Compaction runs only after at
     least 500 deleted detail rows and at least 25% free pages.
   - Distinct requisitions with similar titles remain identity diagnostics, not
     persistence anomalies; treating them as routine prevents ordinary
     full-time postings from filling the hard detail ceiling.
   - Non-intern traces also suppress stale categorical scoring explanations;
     their final reason and watcher diagnostics now consistently report
     `not_internship` without graduate-only evidence.
   - Offline backend/watcher validation: `666 passed, 1 warning`; Python
     compileall and `git diff --check` completed successfully.
   - An isolated 20,000-rejection migration retained exact aggregates and 25
     routine details per run, preserved notification/health/alert state, and
     compacted its temporary database from 40.95 MiB to 0.22 MiB.
26. Evidence-backed maintenance audit:
   - Confirmed both JSON endpoint paths raised an uncaught `RecursionError` for
     payloads beyond the decoder nesting limit. `_json_object` now returns the
     existing HTTP 400 client-error response for that case.
   - Consolidated identical benchmark CLI date/count parsing and source
     provenance counting in `scripts/scoring_benchmark_common.py`; all three
     exporters retain their names and behavior.
   - Retained adapter fetch wrappers, separate health/alert SQLite stores, and
     the standalone alumni-map row builder because they preserve intentional
     isolation or script boundaries.
   - Malformed/Unicode fuzz checks passed for source sanitizers, safe URL
     rendering, and posting identity helpers.
   - Offline backend/watcher validation: `671 passed, 1 warning`; frontend:
     `23 passed`; production build and Python compileall completed successfully.
27. Master-data eligibility false-positive correction:
   - Bare `master` now counts as graduate-degree terminology only with explicit
     degree, program, student, or candidate context. Operational phrases such
     as `master data management`, `master database`, `master record`,
     `master dataset`, and `master schedule` remain categorically eligible.
   - Exact Bosch and generic master-data title regressions pass, while
     `Currently pursuing a master's degree` and `Master's students only`
     remain `graduate_only`.
   - Offline backend/watcher validation: `680 passed, 1 warning`; Python
     compileall and `git diff --check` completed successfully.
   - Safe Actions run `30511581407` completed successfully on
     `43bc3806d58821161127696f2ce856dcdd084418` with email, priming, health
     email, and Workday probe disabled. Its complete categorical audit retained
     only the genuine Capital One master's and PhD examples.
   - Final heartbeat reported `notification_mode=dry_run`, `sent=no`,
     `seen_marked=0`, `health_email_mode=off`, `health_alert_sent=no`,
     `errors=0`, and `seen_loaded=100, seen_saved=100`.
   - All persisted notification rows and timestamps were unchanged. Bosch
     `Autonomous Driving – Internship in Machine Learning` remained pending
     with null `emailed_at` and `primed_at`.
28. Watcher timing instrumentation:
   - Every attempted direct ATS and configured GitHub backstop fetch emits one
     `SOURCE-TIMING` INFO record from a `finally` block with sanitized
     company/adapter/source identifiers, success, three-decimal seconds, and
     row count. Workday records also expose request and retry counts.
   - `STAGE-TIMING` records cover configuration/startup, direct collection,
     GitHub collection, total collection, health persistence, analysis,
     filtering/eligibility, alumni work, seen partitioning, digest/email
     handling, source-comparison generation/persistence, health-alert
     evaluation, and total runtime.
   - Timing remains log-only; heartbeat and health-report shapes are unchanged.
     Offline backend/watcher validation: `709 passed, 1 warning`.
   - An isolated email/prime/health-email-disabled dry run used a fresh
     temporary database and empty injected alumni map. It completed in 459.503
     seconds with 11,897 fetched rows, 11,894 scored jobs, 14 dry-run matches,
     both GitHub feeds successful, and zero `seen` rows.
   - The slowest measured stages were analysis (237.501s), direct collection
     (197.301s), and source-comparison generation/persistence (19.648s). The
     slowest sources were Capital One Workday (36.913s, 1,744 rows, 88
     requests) and Bosch SmartRecruiters (28.869s, 4,739 rows).
29. Posting-analysis context optimization:
   - One per-posting context now owns the title/description/requirements/
     compensation joins and lowercase variants, requirements-only and full
     technology matches, and profile-skill matches.
   - Technology detection runs twice rather than three times per posting;
     profile skills run once rather than twice. Profile regexes and technology
     patterns are compiled once, and signal evidence reuses stored match
     objects instead of repeating successful searches.
   - Context-aware classification, red/positive signals, profile matching, and
     scoring remain optional internally so regression tests can compare the
     context-free path. A 74-row representative corpus produced identical
     serialized jobs and dedupe reports.
   - Offline Windows benchmark results: 500 rows 6.555s versus 7.400s (11.4%
     faster), 1,000 rows 13.004s versus 14.658s (11.3%), and 2,000 rows
     26.244s versus 29.307s (10.5%).
   - Full validation: backend/watcher `713 passed, 1 warning`; frontend
     `23 passed`; frontend production build and Python compileall succeeded.
30. Persistent watcher static-analysis cache:
   - Backend ingestion now exposes pure deduplication, one-row static analysis,
     current scoring/final assembly, and stable score-sort functions. The
     regular `analyze_rows()` and CSV/API paths compose those functions without
     importing watcher configuration or SQLite.
   - `watcher/analysis_cache.py` fingerprints only static analyzer inputs with
     deterministic sorted JSON and SHA-256, including the complete loaded
     profile and known-company configuration plus
     `STATIC_ANALYSIS_CACHE_VERSION`.
   - Batched reads, one transactional artifact write, and one 30-day
     last-access cleanup use `analysis_cache` inside the existing
     `seen.sqlite`. Invalid JSON/schema entries and SQLite failures warn and
     fall back to fresh analysis.
   - Every deduplicated row is scored and assembled from its current row and
     effective date, including current merged provenance. Cache hits therefore
     preserve scores, deadlines, eligibility, IDs, ordering, filtering,
     comparisons, notifications, and dedupe reports.
   - One safe `ANALYSIS-CACHE` INFO record reports rows, hits, misses, invalid
     entries, writes, hit rate, lookup time, static-analysis time, and scoring
     time without keys, URLs, descriptions, or configuration contents.
   - The 2,000-row offline benchmark measured disabled 24.877s analysis /
     25.149s total, empty 24.904s / 25.313s, and warm 5.401s / 5.780s. Warm
     cache had 2,000 hits, no misses, a 100% hit rate, and a 77.0% total-time
     improvement; the SQLite increase was 13,950,976 bytes and all serialized
     jobs/dedupe reports were identical.
   - Full validation: backend/watcher `739 passed, 1 warning`; frontend
     `23 passed`; frontend production build and Python compileall succeeded.
31. Versioned collection snapshot/replay:
   - `watcher/collection_snapshot.py` owns the immutable batch schema,
     collection-only SHA-256 fingerprint, full load validation, and atomic
     UTF-8 gzip JSON persistence.
   - `collect_batch()` is the normal live collector; legacy `collect_rows()`
     remains a compatible wrapper. Live and replay runs then share one
     analysis/filtering/alumni/selection/comparison pipeline.
   - Replay is permanently dry and network-free. It computes source health and
     comparison diagnostics in memory, never records health attempts, alerts,
     seen/prime markers, or comparison runs, and uses the captured UTC date
     unless `--today` overrides it.
   - Snapshot loading is independent of scoring, profile, filtering, alumni,
     email, and cache settings. Static-analysis caching remains available on
     replay and updates its own current access time.
   - Offline regression coverage includes lossless/order-preserving round
     trips, deterministic pipeline equivalence, no-network/no-side-effect
     replay, live capture continuation, config mismatches, profile/scoring
     independence, date override, corruption/version validation, and atomic
     replacement.
   - The isolated 11,880-row benchmark measured live dry capture at 419.823s
     total / 192.984s analysis, disabled-cache replay at 215.480s / 191.733s,
     and warm replay at 74.877s / 50.880s with 11,877 hits and no misses.
     The compressed snapshot was 5,695,926 bytes and deterministic outputs were
     identical. Network was degraded for the live leg: six of 26 Workday
     tenants succeeded and 20 reported `network_failure`.
   - Full validation: backend/watcher `756 passed, 1 warning`; frontend
     `23 passed`; frontend production build and Python compileall succeeded.
32. Evidence-first correctness and redundancy audit:
   - A failing regression proved that an explicit dry `run_once()` still
     invoked the digest sender before discarding its return value. Dry mode now
     skips email transport entirely while preserving pending-match reporting
     and notification state.
   - A malformed bracketed GitHub feed URL reproduced a raw
     `urllib.parse.ValueError`; configuration now consistently raises the
     documented `ConfigError` for that invalid input.
   - The duplicated quote-aware comment scanners for dotenv and watchlist
     parsing now share one covered helper. Analysis-cache timestamp handling
     reuses the existing watcher UTC normalization helpers.
   - AST-based dead-function, unused-import, and exact-duplicate scans found no
     additional safe deletions. Intentional package re-exports, context-manager
     protocol methods, and layer-specific helpers were retained.
   - Full validation: backend/watcher `758 passed, 1 warning`; frontend
     `23 passed`; frontend production build and Python compileall succeeded.
33. Collection snapshot schema-v2 hardening:
   - Snapshot batches now preserve aggregate Workday request attempts alongside
     tenant outcomes, retries, and failure codes. The schema version increased
     because the persisted structure changed.
   - Snapshot writes use sorted compact JSON in a filename-free, zero-mtime
     gzip stream. A regression proves identical batches produce identical
     compressed bytes, while atomic replacement behavior remains covered.
   - Strict loading rejects unknown fields as well as corrupt, truncated,
     malformed, structurally invalid, and unsupported-version files.
   - A fresh isolated production-sized capture retained 11,858 rows in
     5,689,148 bytes. Live collection took 208.299s and total runtime was
     388.333s with 158.718s analysis. Disabled-cache replay took 179.099s total
     / 159.124s analysis; warm replay took 77.151s / 52.365s with 11,855 hits,
     no misses, and a 100% hit rate.
   - Deterministic jobs, dedupe reports, matches, and in-memory comparison were
     identical. Replay skipped collection, left operational SQLite state
     unchanged, sent no email or health alerts, marked no seen rows, and
     persisted no comparison run. The permitted analysis cache remained active.
   - Full validation: backend/watcher `762 passed, 1 warning`; frontend
     `23 passed`; frontend production build and Python compileall succeeded.
34. Fully warm collection-replay performance audit:
   - A benchmark-only profiler replayed the 5,689,148-byte production snapshot
     at the fixed `2026-07-30` date. It used a prewarmed disposable cache, one
     unmeasured warm-up, three measured full runs, three source-comparison
     omission runs, three isolated assembly passes, and one cProfile run.
   - The 11,858 collected rows deduplicated to 11,855 jobs. Measured totals were
     63.464s, 63.546s, and 63.253s (63.464s median); every run had 11,855 cache
     hits, no misses, no socket/DNS or collection calls, the same deterministic
     output hash, and unchanged non-cache SQLite state.
   - Median current-date scoring/final assembly was 42.883s. Full
     source-comparison construction measured 16.297s directly and 16.359s by
     omission. Fingerprinting, batched SQLite lookup, and cached JSON
     decode/validation took 0.231s, 0.675s, and 0.208s; unexplained runtime was
     0.263s.
   - cProfile attributed 35.883s cumulative profiler time to backend student
     eligibility inside 54.287s of assembly, with repeated regex search and
     normalization dominating call volume. The next candidate is a versioned
     static scoring/eligibility artifact that leaves deadline scoring and final
     date-relative composition dynamic; no production optimization was made.
35. Versioned static scoring and eligibility artifact:
   - The pure backend artifact and watcher cache version are now `2`. The
     existing artifact stores one student-eligibility decision, normalized
     evidence and parsed qualification segments, seven date-independent score
     category results, role-ineligibility input, and red-flag cap inputs.
   - Dynamic assembly still recomputes deadline/expiration, category weights
     and totals, caps, final eligibility, bucket/actions, reasons/concerns,
     current IDs and row/provenance fields, and final sorting. Backend CSV/API
     analysis composes the same pure builder and never imports SQLite.
   - Fingerprints now include canonical location/remote values and only the
     structured eligibility inputs selected by backend policy. Volatile source
     diagnostics, current provenance, deadline/date values, and dynamically
     applied watcher target roles remain excluded.
   - The fixed 11,855-job replay measured 204.310s cache-disabled, 209.714s
     cold, and 28.224s warm. Warm dynamic scoring/assembly was 0.909s versus
     the 42.883s baseline (97.9% faster); total replay improved 55.5%.
     Fingerprinting took 1.005s and lookup/JSON validation took 2.071s.
   - Source comparison was unchanged and measured 20.308s in the isolated
     benchmark. The artifact database grew from 89,657,344 to 123,641,856
     bytes. Disabled, cold, and warm output hashes were identical; all replay
     cases made no network calls and left operational SQLite state unchanged.
   - Full validation: backend/watcher `770 passed, 1 warning`; frontend
     `23 passed`; frontend production build and Python compileall succeeded.

36. Opt-in bounded collection concurrency:
   - **Production default remains serial; concurrent mode is available for
     controlled canaries.** `WATCHER_COLLECTION_MODE` defaults to `serial`,
     serial stays the permanent rollback/diagnostic path, and promotion is a
     separate reviewed change.
   - Validated limits are `WATCHER_COLLECTION_MAX_WORKERS` (1-16, default 4),
     `WATCHER_WORKDAY_MAX_CONCURRENCY` (1-5, default 1), and
     `WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY` (1-4, default 2). Neither
     scope limit may exceed the worker pool, and a task starts only when the
     global pool, its origin, its provider, and the Workday limit all allow it.
   - Origin/provider keys carry only scheme, host, and port. Companies sharing
     an ATS host share one origin limit; each Workday tenant is its own origin
     under the shared Workday limit. Collection plans in configuration order,
     executes under the active mode, and applies outcomes in that order, so
     rows, errors, attempts, counters, and downstream output are unchanged.
   - Production collection builds one adapter set per worker thread and shares
     one thread-safe Workday tenant pacer. Timeouts, retries, backoff, and
     pacing are unchanged, and there is no proxy, cookie, header-rotation,
     browser-automation, or challenge-bypass behavior. Escaped worker errors
     become failed task results with sanitized exception types.
   - Stage 1 (deterministic offline, 42 fake delayed sources, 0.05s each,
     limits 4/1/2) passed every check: identical batch digests, byte-identical
     snapshots, identical downstream pipeline fixtures, preserved row/attempt/
     error order, observed maxima 4 global / 2 per origin / 2 provider / 1
     Workday, serial peak 1, failure isolation, isolated worker programming
     errors, clean shutdown, no seen rows, no email. Wall clock was 2.131s
     serial versus 0.715s concurrent (2.98x).
   - Stage 2 (limited live canary, 2026-07-31, limits 4/1/2) used one company
     per adapter type with one Workday tenant: Capital One, Anduril Industries,
     Bosch, Canary Technologies, Chainalysis, ICEYE. Result: 6 attempted,
     5 successful, 1 valid empty, 0 failed, 0 HTTP 401/403/429, 0 challenges,
     87 Workday requests with 0 retries, 8,684 rows in 37.087s, observed maxima
     4 global / 1 per origin / 1 Workday, clean shutdown, 0 unexpected
     exceptions, production state unchanged.
   - Stage 3 (full live canary, 2026-07-31, limits 4/1/2) collected the full
     configured source set plus both GitHub feeds: 61 attempted, 40 successful,
     1 valid empty, 20 failed, 11,872 rows in 159.861s, observed maxima 4
     global / 2 per origin / 2 provider / 1 Workday, 0 HTTP 401/403/429, 0
     challenges, 0 unexpected exceptions, clean shutdown, production state
     unchanged (`watcher/seen.sqlite`, `watcher/analysis-cache.sqlite`, and
     `.watcher-state/seen.sqlite` fingerprints identical before and after).
   - All 20 full-canary failures were Workday `network_failure` with 0
     non-Workday failures. Six of 26 tenants succeeded (Capital One,
     Salesforce, Cornerstone Research, FTI Delta, Thornton Tomasetti, Eli
     Lilly), exactly matching the serial baseline recorded in item 31 (six of
     26 tenants succeeded, 20 `network_failure`). Retries were 40 for 20 failed
     tenants, which is exactly two per failure under the unchanged three-attempt
     policy, so Workday retries did not materially increase. This is
     pre-existing environmental Workday behavior on this host, not a
     concurrency regression, and no source-level blocking signature exists.
   - Historical serial baseline, not a fresh back-to-back serial run: item 33
     measured 208.299s live collection for 11,858 rows and item 28 measured
     197.301s direct collection. In-run summed fetch time was 206.26s versus
     159.861s wall clock (22.6% faster). Workday alone accounted for 156.18s of
     serialized fetch time at Workday concurrency 1, so the Workday chain is the
     critical path and bounds the achievable speedup on this source set. No
     limit increase is recommended without further canary evidence.
   - Promotion evidence still required before a separate, small, reversible
     default change: at least three successful full concurrent canaries in
     separate normal collection windows, no material source-failure increase, no
     material Workday retry increase, no new rate-limit or challenge behavior,
     comparable per-source row coverage, complete source-attempt diagnostics,
     identical deterministic fixture outputs, stable ordering and deduplication
     precedence, clean executor shutdown, and no operational-state side effects.
     One full canary exists as of 2026-07-31; two more are outstanding.
   - Full validation: backend/watcher `835 passed, 1 warning`; Python
     compileall and `git diff --check` completed successfully.

37. Additional full-canary validation:
   - At `2026-07-31T07:16:55Z` (`2026-07-31T03:16:55-04:00`), local branch
     `main`, local `HEAD`, cached `origin/main`, and a fresh read-only GitHub
     remote-tip query all resolved to
     `10d4f89e49e168d44b6b52054ce6aed749a143a9`.
   - The concurrency implementation is present in the working tree:
     `watcher/collection_concurrency.py`, configuration parsing/validation,
     deterministic collection planning/reduction in `watcher/run.py`, the
     offline benchmark, staged canary harness, and their tests and
     documentation. Focused offline verification passed `96` tests.
   - Both full canaries described here ran before the implementation was
     committed or pushed. At that time, the working tree had 24 modified
     tracked files and seven untracked files, including the concurrency module,
     benchmark/canary scripts, and both concurrency test modules. Their results
     remain operational evidence with explicitly uncommitted provenance.
   - The prior full canary ended at 06:17 UTC. Scheduled serial Actions run
     `30610654239` then ran in the intervening normal window from 06:46 to 06:56
     UTC; its watcher step succeeded, while the overall job failed only while
     saving the data branch. No fresh serial run was started around this
     canary.
   - The first requested additional full canary started at
     `2026-07-31T08:13:32.529171Z` (`04:13:32-04:00`), run ID
     `20260731T081332Z-9d878f94901e`, with fixed limits 4 global / 1 Workday /
     2 per origin and collection fingerprint
     `71002a66c3de7aa084fb84c6b270e94ef73cca95490d4ff4a6dba583c27967c1`.
     It passed: 61 attempted, 40 successful, one valid empty, 20 failed,
     11,872 rows, 0 HTTP 401/403/429, 0 challenges, 0 unexpected exceptions,
     observed maxima 4/2/2/1, and clean executor shutdown. Collection took
     163.553s; the 61 logged source timings summed to 208.159s, saving 44.606s
     (21.4%).
   - All failures were the same 20 Workday `network_failure`s as the earlier
     local canary. The successful set remained Capital One, Salesforce,
     Cornerstone Research, FTI Delta, Thornton Tomasetti, and Eli Lilly.
     Workday made 155 requests and 40 retries: two retries for each failed
     tenant and none for successes. No non-Workday source failed. The harness
     did not retain per-tenant start timestamps, so the configured 0.5s pacing
     interval and Workday peak of one are verified, but an observed minimum
     spacing and explicit violation count are unavailable.
   - Workday start telemetry is now implemented in memory: the shared pacer
     records each monotonic tenant start after pacing and directly before the
     adapter fetch, then reports the configured interval, count, minimum/
     median/maximum spacing, numeric violations, and deterministic sanitized
     company/task offsets. It sleeps without holding the coordination lock and
     does not extend snapshots, SQLite, health, heartbeat, or email schemas.
     Focused concurrency, canary, and Workday transport tests pass (`103`).
   - Versus the earlier concurrent canary, every source outcome was identical;
     Capital One changed by +1 row and LinkedIn by -1, leaving the total
     unchanged. Versus intervening serial run `30610654239`, the 20 Workday
     tenants had succeeded and contributed 5,587 rows; the repeated local
     failure split is classified as the previously observed Workday
     environmental pattern. The only other differences were Bosch -2,
     LinkedIn -1, and the Markdown feed -4, classified as expected posting
     changes.
   - The immutable snapshot SHA-256 is
     `1b3c47ebb979df659f0c1b37505c391ba85169f32434ca8fdfa1a87e40ef7bd8`.
     Offline isolated processing deduplicated 11,872 rows to 11,869 jobs,
     recorded three duplicates/cross-source merges with direct ATS precedence,
     and found 10 eligible matches. Three warm replays had 11,869 hits, zero
     misses/network calls, identical output hash
     `169500fca6f4272d3525e295e28ea3fe38a7e9a31295cd3ad0515769e9d7029d`,
     median total 9.316s, and median source comparison 2.842s. Isolated
     comparison persistence took 0.017s.
   - All three script-tracked production paths remained absent, and every
     additional local SQLite artifact retained its exact pre-run SHA-256. The
     canary and replay invoked no email, seen marking, priming, durable health
     write, production comparison/cache write, or other production mutation.
   - Detailed evidence is gitignored at
     `evaluation/private/canary-full-concurrent-20260731-window2.json`,
     its adjacent snapshot/downstream report, and
     `evaluation/private/concurrent-canary-window2-assessment-20260731.json`.
     Two full operational canaries have passed in separate windows; one remains
     outstanding. This bounded change publishes the reviewed implementation;
     no promotion action is included and production remains serial.
   - Final staged-tree validation passed: backend/watcher `823 passed, 1
     warning`; frontend `23 passed` plus the production build; Python
     compileall; workflow YAML parsing; and `git diff --check`. The 42-source
     offline benchmark preserved byte-identical batches/snapshots, downstream
     output hashes, row/error/attempt order, limits 4/2/2/1, zero Workday pacing
     violations, zero operational-state writes, and clean shutdown.

38. Scheduled bounded-concurrency promotion:
   - Three full concurrent canaries passed in separate collection windows at
     fixed limits of four global workers, one Workday task, and two tasks per
     origin (`4/1/2`). The third canary ran from the clean, pushed
     `d255c9b1623b7192ad83a48cd28d3ce5b90b7c3f` implementation and retained
     Workday start-spacing telemetry with zero pacing violations.
   - The scheduled watcher workflow now explicitly selects `concurrent` mode
     at `4/1/2`. The application's built-in default remains `serial`, and
     rollback requires changing only `WATCHER_COLLECTION_MODE` in the workflow
     to `serial`.


## Next

- Keep the labeled `scoring_us_rolefit_20260726` inputs frozen; use report-only
  reevaluation for separately scoped production changes.
- Commit the independent holdout tooling, then collect the holdout only from
  that exact clean committed SHA.
- Use manual `send_email=false, prime_seen=false` for a notification-state-safe
  dry run. Use `prime_seen=true` only for an intentional one-time suppression.
- After confirming the data branch exists and the heartbeat looks right, set
  the repo Actions variable `WATCHER_SEND_EMAIL=true` to enable scheduled sends;
  leave `WATCHER_PRIME_SEEN` unset/false for normal operation.
- Keep source-health mode and internship-match send mode independently
  configured; use manual `health_email_mode=off` for transport probes.

- Inspect the first scheduled production run at fixed `4/1/2`; return only
  `WATCHER_COLLECTION_MODE` to `serial` if concurrency introduces new blocking,
  reliability, ordering, pacing, shutdown, or diagnostic regressions.

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

Latest local validation after collection snapshot schema-v2 hardening:

```text
762 passed, 1 warning in 14.57s
```
