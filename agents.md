# Agents Guide

## Current integration

- `product-mvp` is the hosted multi-user product branch.
- Current task: extract the four current watcher configuration dataclasses and
  intrinsic constants into `watcher/config/models.py`. Keep environment,
  loading, and validation in the stage-one transitional module, preserve the
  `watcher.config` facade, make one isolated commit, and do not push.
- Phase 3A is complete, and Phase 3B scheduling and automation remain paused.
- Personal scoring, alumni-specific behavior, and internal-only workflow changes are out
  of scope on this branch.
- Shared watcher and core fixes must arrive as reviewed, isolated commits.

## Hosted per-user matching (Phase 2B)

- `hosted_user_job_matches` (Alembic `20260803_0003`) stores one durable row per
  `(user_id, job_id)` with bounded JSONB reasons, `matched_at`,
  `last_matched_at`, and nullable `no_longer_matches_at`, `saved_at`, and
  `dismissed_at`. Rows cascade with the account and are never deleted when
  preferences change.
- `hosted/matching.py` is pure and database-free. All hard constraints must
  pass: watched and unpaused company, open job, selected hosted role,
  location/remote compatibility, then season compatibility. Never use watcher
  fit scores, LLM or fuzzy ranking, resume inference, personalized ranking,
  alert frequency, the global pause, or email-delivery settings.
- Season is derived from posting titles only; titles without season evidence
  stay compatible, matching the repository rule that ambiguity passes.
  `include_remote=false` excludes remote postings outright.
- Reasons use allowlisted codes carrying only catalog identifiers. Never store
  descriptions, raw preference payloads, secrets, or source metadata.
- `hosted/match_service.py` reconciles by job (import) and by user
  (preferences, company watches), restricting candidates by company watch and
  existing rows rather than scanning all users by all jobs. Repeat passes with
  unchanged inputs write nothing.
- Import reconciliation runs inside the job-persistence transaction.
  `matches_created` counts only newly inserted rows — never reactivations,
  timestamp-only updates, or saved/dismissed changes — and `already_imported`
  changes no rows or timestamps.
- `GET /api/matches`, `GET /api/matches/{id}`, and `PATCH /api/matches/{id}`
  are ownership-scoped: another user's UUID returns the ordinary 404. The list
  defaults to active, undismissed matches with bounded `view`, `limit`, and
  `offset`. PATCH accepts only strict-boolean `saved`/`dismissed`.
- Save and dismiss are independent states; dismissing never clears a save.
- Live frontend mode calls the real endpoints; mock mode keeps working and must
  display the demo-data banner, which live mode must never show.

## Hosted notification delivery (Phase 3A)

- Import-only notification enqueueing runs in the same PostgreSQL transaction
  as job persistence and Phase 2B reconciliation. Only newly inserted match
  rows for active, verified, unpaused users create items; reconciliation,
  reactivation, and `already_imported` never enqueue backlog.
- `hosted_notification_batches`, `hosted_notification_items`, and
  `hosted_notification_attempts` own delivery state. As-detected batches group
  by user/import; three-hour and daily batches use rolling windows from the
  first item. Only unclaimed pending batches accept new items.
- `python -m app.hosted.deliver_notifications --limit 25` is one-shot. Claims
  use `FOR UPDATE SKIP LOCKED`, random tokens, and 10-minute leases, with no
  transaction held during SMTP. Expiry before submission returns to pending;
  expiry after `send_started_at` becomes non-retryable `uncertain`.
- Retryable failures wait 1, 5, 15, then 60 minutes and stop after five
  attempts. Permanent failures stop immediately; ambiguous post-submission
  outcomes are never automatically retried. Store only allowlisted codes and
  reuse the batch's deterministic Message-ID.
- Delivery revalidates account, preferences, matches, and open jobs. Digests
  use the hosted SMTP settings, resolve the verified address at send time, and
  never store/log recipients, bodies, raw SMTP errors, descriptions, or source
  metadata. Watcher SQLite and watcher email state remain separate.

## Required every prompt

- After every user prompt, update the root `agents.md`, `claude.md`, and
  `.gitignore`. Keep them concise, synchronized, and relevant.
- Preserve unrelated user changes. Do not create case-variant instruction files
  or duplicate watcher handoffs.

## Sources of truth

- Before watcher work, read `WATCHER_SPEC.md` in full.
- Use `WATCHER_PROGRESS.md` for current status, `README.md` for operations, and
  `evaluation/README.md` for scoring-benchmark instructions.
- Record watcher progress only in the root `WATCHER_PROGRESS.md`.

## Scope and architecture

- `internship_signal/domain/` owns dependency-light concepts shared by backend
  and watcher: canonical job columns, identity normalizers, and categorical
  eligibility reason codes. It imports neither application layer; legacy
  backend owners re-export the same objects.
- Change only the layer the user requested. Do not add adapters, scheduling, or
  workflow changes without explicit scope.
- Keep CSV parsing/cleaning in `backend/app/ingest.py::process_csv`.
- Keep canonical-row scoring in `backend/app/ingest.py::analyze_rows`. Reuse
  backend classification, salary parsing, signals, dedupe, scoring, and IDs.
- Preserve `process_csv` output shape, cleaning report, scoring, and ordering.
- `backend/app/ask.py` is deterministic. Backend-specific queries require a
  backend-adjacent `role_track` or the existing `backend_focus` signal.
- Keep all text I/O explicitly UTF-8.

## Backend safety

- Use `backend/app/main.py::_json_object` for JSON endpoints. Malformed or
  excessively nested JSON, non-object bodies, and non-string
  `csv_text`/`question` values are HTTP 400.
- Multipart `file` must be an upload. CSV input is limited to 10 MiB; oversized
  input is HTTP 413.
- Keep frontend CSV formula-injection protection.
- Invalid optional `KNOWN_COMPANIES_PATH` data falls back to built-ins.

## Watcher config and sources

- `watcher/config/models.py` owns the dependency-light configuration
  dataclasses and their intrinsic defaults/value constants. The stage-one
  `watcher.config` facade retains every current import, while dotenv,
  environment, loading, and validation remain in `config/_legacy.py`; process
  environment values win.
- Production watchlists require nonblank `defaults.terms`. Company overrides
  may inherit but may not be empty.
- GitHub feed URLs must be nonblank HTTP(S), credential-free, and distinct
  after removing query/fragment. Do not hard-code recruiting-year feeds.
- GitHub backstops use typed `github_listing_sources`; keep legacy
  `github_listing_urls` compatible. Merge direct ATS, Simplify JSON, then
  Markdown sources by fixed priority, independent of configuration order.
- Markdown `Added` dates are source metadata, never employer `date_posted`;
  source markers may add restrictions but lower-priority closed state must not
  override an active direct posting.
- Source provenance keys off `extra.source_adapter`, which `make_row` always
  sets. CSV `extra` holds unmapped user columns (a `source` column collides with
  the `source_url` alias) and must never drive dedupe ordering or provenance.
- `watcher/sources/registry.py` is the single registration point for direct
  ATS adapters and runtime construction. Config validation derives its direct
  values from `DIRECT_ATS`; `bespoke` and `github_only` remain non-direct.
- Shared source ownership is split across `contracts.py`, `diagnostics.py`,
  `transport.py`, `parsing.py`, `rows.py`, and `sanitize.py`; `base.py` is the
  compatibility facade and `retry.py` owns bounded retry mechanics.
  Workday's registry entry alone requests the shared pacer.
- Adapters live in `watcher/sources/`, return canonical rows, and set
  `extra.source` plus `extra.source_adapter`. GitHub rows also keep safe
  `extra.feed_url`.
- Retain valid postings from mixed malformed payloads and emit one bounded
  warning. Nonempty all-malformed payloads fail. Paginated sources reject
  repeated pages.
- Adapter tests use saved fixtures and never the network. Verify live endpoints
  manually before adding or changing an adapter, then save a safe fixture.
- `watcher.detect` is manual-only and must never fabricate ATS settings.

## Watcher run, scoring, and state

- Watcher timing uses `time.perf_counter()` and stable INFO
  `SOURCE-TIMING`/`STAGE-TIMING` logs emitted from `finally`; keep identifiers
  URL/secret-free, show three decimals, and do not alter heartbeat schemas or
  watcher behavior.
- Performance audits distinguish offline pytest time from live watcher time;
  use timing logs, test durations, and isolated profiles before proposing an
  optimization, and keep diagnosis read-only unless a fix is requested.
- Analysis optimizations reuse a per-posting text/match context and one
  compiled profile-skill matcher per loaded profile; context-free callers and
  serialized scoring output must remain exactly compatible.
- Benchmark analysis changes offline at 500, 1,000, and 2,000 representative
  rows without adding prefilters, collection concurrency, pagination changes,
  or source-comparison redesign.
- Persistent analysis caching is watcher-owned in a dedicated rebuildable
  SQLite database beside durable state; backend analysis stays pure and
  cache-independent, fingerprints are deterministic/versioned, and
  current-date scoring always runs for every deduplicated row.
- Cache corruption, schema mismatch, or SQLite failure is nonfatal and falls
  back to fresh analysis; batch reads, transactional writes, and one bounded
  30-day cleanup per run must preserve byte-identical jobs and dedupe reports.
- `watcher/run.py` fetches direct sources before GitHub so backend dedupe keeps
  direct provenance. `bespoke` and `github_only` skip direct fetching.
- Collection concurrency is opt-in in the application, whose default is
  `serial`; scheduled production explicitly selects `concurrent`. Validate
  global workers (1-16), Workday concurrency (1-5), and per-origin concurrency
  (1-4), with neither scoped limit exceeding the worker pool.
- Plan in configuration order, isolate adapters and mutable diagnostics per
  worker, share only the Workday pacer, and reduce outcomes in plan order.
  Replay creates no executor or network work; executors must shut down cleanly.
- Record actual Workday starts with a monotonic clock after pacing and directly
  before fetch. Sleep without holding the pacer lock. Keep interval, count,
  spacing statistics, numeric violations, and sanitized relative offsets only
  in private canary reports—not snapshots, SQLite, health, heartbeat, or email.
- Validate serial/concurrent batch and snapshot equivalence, ordering, limits,
  isolation, pacing, and zero state writes before promotion.
- In `collect_rows`, only `None` means “construct defaults.” Preserve explicit
  empty injected sources.
- Watcher code must not compute scores or IDs. Backend scoring owns
  `fit_score`, role track, eligibility, explanations, actions, and degree
  decisions.
- `watcher/eligibility.py` is the only watcher-side target-role/degree wrapper;
  `watcher/filters.py` then requires eligible, positive-fit, open
  internships/co-ops and applies optional `min_score`.
- Categorical student eligibility exclusions live only in
  `watcher/eligibility.py`. Exclude clear mandatory `phd_only`,
  `graduate_only`, `freshman_only`, and `returning_intern_only` restrictions;
  mixed, preferred, incidental, and ambiguous evidence remains eligible.
- Operational terms such as `master data`, `master record`, and
  `master schedule` are not graduate-degree evidence; bare `master` requires
  explicit degree, program, student, or candidate context.
  Apply categorical rules only after internship/student-program and open-status
  checks; traces retain mandatory, negation, and mixed-evidence diagnostics.
- `watcher/eligibility.py::assess_us_location` owns the conservative location
  gate. Explicit U.S. evidence wins across multiple locations; explicit
  foreign country/region evidence yields `outside_us`; ambiguous text passes.
  Prefer already-collected structured country/location metadata over
  free-form text, keep city-only evidence ambiguous, and preserve diagnostic
  evidence. Location eligibility must not change backend fit scores, actions,
  or roles.
- Graduate/advanced-degree internships are excluded from digests with
  `watcher_eligible=false` and `fit_score=0`.
- Role classification prioritizes title and core duties over incidental
  keywords. Admit technical AI-integration, digital-solutions, quant, product,
  and umbrella-program roles only with explicit central technical evidence;
  physical engineering, consumer research, and manufacturing quality remain
  excluded.
- IT support, quality/test, and solutions engineering are deliberate
  low-priority exceptions capped around 20 unless explicitly changed.
- `watcher/season.py` warns on non-`ok` season status but never blocks direct
  collection.
- `watcher/seen_store.py` uses analyzed job IDs. GitHub/direct sightings of the
  same job are not new. Batch marking is transactional.
- Mark emailed only after a successful live send; explicit `--prime-seen`
  writes the separate priming marker.
- Dry runs never change notification state. Explicit priming uses a distinct
  persisted marker, while live sends populate `emailed_at` only after success;
  legacy rows with neither marker remain pending.
- Collection dedupe and seen suppression share one posting-identity policy:
  stable source requisition ID, then posting-specific normalized URL, then a
  conservative company/title/location fallback. Generic URLs never collapse
  distinct stable requisitions.
- Digest policy: no default score gate; exclude ineligible jobs; sort by fit,
  generic score, role priority, company, and title; send nothing for zero new
  matches.
- `watcher.audit` is read-only: state-only mode makes no requests, live mode
  reuses normal collection/analysis with email and priming disabled, and
  neither mode may mutate `seen` or health history.
- Source comparison computes lightweight outcomes/counts for every job, then
  selects details before building and sanitizing rich traces. The report owns
  deterministic eligible/anomaly/non-routine/routine-sample retention; the
  store persists selected entries without a second policy pass. Decisive early
  reasons defer rich-only location/notification expansion until selection.
  Keep 30 aggregate runs and three detail runs.
  Health-alert
  fingerprints/cooldowns use dedicated tables, never `seen`; health SMTP is
  independently configured and cannot affect match-email delivery or marking.

## Alumni and private data

- Alumni data is additive only: never gate, reorder, or rescore jobs.
- Ignore private/generated payloads, not reusable helper scripts or test
  fixtures; new code and fixtures must remain visible for review.
- Loading priority is compact JSON env/text/path, then CSV env/path. Live sends
  require usable alumni data; dry runs may report matching disabled.
- Matching order is normalized exact, built-in aliases, watchlist
  aliases/`alumni_match`, then conservative fuzzy fallback.
- Keep `.env`, `private/`, alumni files, SQLite state, health/probe reports,
  downloaded or extracted Actions diagnostics, and `evaluation/private/` out
  of Git. Never log private contacts or SMTP recipients.

## Source health and Workday

- `watcher/source_health.py` is pure and makes no network requests. Record one
  direct outcome per company and one outcome per GitHub feed per run.
- Health state shares `seen.sqlite`; writes are transactional and must not alter
  `seen`. Initialization is not a transition. Health is nonfatal and does not
  affect email or seen marking.
- Track every configured GitHub source independently; a valid parsed feed is
  healthy even when watchlist filtering yields no matches.
- Direct state is exactly `healthy_with_listings`, `healthy_empty`, `degraded`,
  `failed`, `not_configured`, or `unknown`. Bounded adapter diagnostics decide
  completeness; snapshots write schema v3 and load v2 with unknown diagnostics.
- Workday continuation failures and repeated pages preserve prior usable rows
  but mark collection incomplete/degraded; a first-page failure stays fatal.
- Sanitize stored/logged errors, feed labels, keys, reports, annotations, and
  heartbeats. Never include credentials or raw query strings.
- Sanitizers must be total. `sanitize_error`, `sanitize_feed_label`, and
  `sources/base.py::_safe_url` run over arbitrary failure text, so guard both
  `urlsplit` and `parsed.port`; a malformed URL must never abort a run.
- Workday logs only safe metadata; never bodies, cookies, sensitive headers,
  tokens, or challenge content. HTML is a fetch failure, not an empty board.
- Workday retries only transient failures, with three total attempts, bounded
  backoff, capped `Retry-After`, and instance-local tenant pacing. Do not retry
  config or valid-JSON schema failures.
- Do not use cookies, proxies, CAPTCHA bypass, browser automation, or other
  anti-bot evasion. Do not reset `watcher-data`, seen rows, or health history.
- Transport probes use `scripts/probe_workday_transport.py`, set
  `WATCHER_SEND_EMAIL=0`, use no production state, and never prime seen jobs.

## Workflow and benchmark

- The workflow persists `seen.sqlite` on `watcher-data`. Priming uses
  `send_email=false`; the isolated Workday probe uses no state, alumni, or SMTP.
- Rollout verification must dispatch with internship email, priming, Workday
  probe, and health email disabled; preserve notification timestamps and restore
  preexisting repository variables before enabling conservative health alerts.
- Preserve every application `HEARTBEAT:` field. Final workflow diagnostics
  may append scheduled-delivery and persistence fields; a missing heartbeat or
  corrupt seen-store is fatal, while source failures remain warnings.
- Scheduled send configuration distinguishes recognized true/false,
  missing/blank, and invalid values. Disabled schedules warn with the pending
  count; intentional manual dry runs do not receive that warning.
- Benchmark export uses `collect_rows()` then `analyze_rows()`. Candidates use
  only `is_internship()` and `is_open()`, never `filter_matches()`.
- Keep international rows in frozen benchmark exports. Current benchmark
  predictions apply the same production watcher eligibility helper while
  preserving scoring and ranking diagnostics.
- Treat every existing `scoring_20260724_*` artifact as immutable. Build the
  separate U.S. role-fit set with `build_us_rolefit_benchmark.py`; its candidate
  pool includes only `us`/`ambiguous` location decisions and its cohorts are
  `random`, `likely_match`, and `difficult_negative`.
- Freeze U.S. role-fit artifacts only from a clean committed implementation;
  require `git_dirty=false`, verify frozen-input hashes, and compare any rebuild
  against the prior exact IDs, memberships, and location-status distribution.
- Benchmarking is measurement only: do not change scoring behavior. Keep labels
  blind, sampling deterministic, frozen rows whitelisted, and evaluation
  offline with exact IDs/date. Only the random cohort supports headline
  population metrics.
- Benchmark labels use required `human_eligible` values `yes`, `no`, or
  `uncertain`; role track, exclusion reason, and notes remain optional.
  Uncertain rows are counted but excluded from binary metrics.
- Store benchmark output only in `evaluation/private/`; never load alumni, send
  email, use seen state, touch `watcher-data`, or upload artifacts.

## Validation

Backend and watcher:

```bash
PYTHONPATH=.:backend backend/venv/Scripts/python.exe -m pytest backend/tests watcher/tests -q
PYTHONPATH=.:backend python3 -m compileall -q internship_signal backend watcher scripts
```

WSL fallback for the Windows virtualenv:

```bash
cmd.exe /C "cd /D C:\Users\burst\internship-signal && set PYTHONPATH=C:\Users\burst\internship-signal;C:\Users\burst\internship-signal\backend && backend\venv\Scripts\python.exe -m pytest backend\tests watcher\tests -q"
```

Frontend:

```bash
cd frontend
npm test
npm run build
```

If WSL Node fails on `/mnt/c`, run:

```bash
cmd.exe /C "cd /D C:\Users\burst\internship-signal\frontend && npm test -- --run && npm run build"
```

Always finish with:

```bash
git diff --check
git status --short --ignored
```

- Bug audits require a reproducible failure or clear violated invariant. Add a
  regression test before fixing behavior; do not change code for style alone.
- Keep the current watcher-audit fix separate from hosted UI/auth fixes, and
  preserve every pre-existing dirty hunk in both worktrees.
- Local commits stay on their dedicated watcher and hosted branches; never
  push them without a separate explicit request.
- Push dedicated branches by explicit name with normal upstream tracking;
  never force-push or substitute another branch.
- When Windows Git cannot resolve a WSL-created worktree pointer, push the
  named shared branch through the valid original checkout without repairing it.
- A branch push is complete only when `ls-remote` reports the intended full
  commit SHA; failed read-only worktree attempts do not alter repository state.
- Main integrations start from a freshly fetched `origin/main`, merge the
  named feature branch, and exclude unrelated local-main-only commits.
- Repository-wide readability cleanup must preserve public shapes, ordering,
  logs, and side effects; consolidate duplication only after tests cover every
  caller.
- Cleanup audits require an executable failure or a clearly demonstrated
  invariant violation before behavior changes. Remove code only after caller
  searches and regression tests prove it is redundant or unreachable.
- Parallel handoffs preserve dirty work on a unique branch without stashing,
  resetting, cleaning, or touching another worktree; stage only owned paths or
  hunks.
- Holdout construction is two-stage: commit reusable tooling first, then
  collect from that exact clean SHA. Exclude both prior benchmarks by stable
  ID, normalized URL, and fallback key without reading their human labels.
- Collection snapshots are immutable, versioned gzip JSON batches produced by
  live collection and consumed by the same post-collection pipeline as live
  runs; writes are atomic and loaded structures are validated before analysis.
- Snapshot fingerprints include only collection-affecting watchlist/source
  configuration. Scoring, profile, filtering, alumni, email, and analysis-cache
  changes remain replay-compatible.
- Replay is network-free and operationally side-effect-free: never email,
  prime or mark seen, persist source health/comparison, or send health alerts.
  It may read/write the static-analysis cache and build diagnostics in memory.
- Replay uses the captured UTC date unless an explicit test date overrides it,
  and collection-configuration mismatches require an explicit CLI override.
- Snapshot benchmarks compare deterministic pipeline outputs and persistent
  state before/after replay; analysis-cache maintenance is replay's only
  permitted write.
- Warm-replay audits use benchmark-only instrumentation, a fixed snapshot/date,
  one warm-up, and at least three measured runs; never add production bypass
  flags while isolating cache, scoring, or source-comparison costs.
- Static scoring-cache artifacts may hold only date-independent eligibility
  evidence and category components; deadline scoring, final decisions, IDs,
  current row/provenance assembly, and sorting always remain dynamic.
- Keep profiler data, snapshots, and generated benchmark reports ignored while
  leaving reusable benchmark scripts, tests, and fixtures trackable.
- Keep rebuildable `analysis-cache.sqlite` transactionally independent from
  durable `seen.sqlite`; only the durable database belongs on `watcher-data`.
