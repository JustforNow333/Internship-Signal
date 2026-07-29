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

Latest local validation after the Actions rollout eligibility fix:

```text
651 passed, 1 warning in 12.29s
```
