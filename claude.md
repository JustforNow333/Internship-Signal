# Claude Repository Guide

- `product-mvp` is the hosted multi-user product branch.
- Current task: port the reviewed reusable classification, recall, and
  filtering fixes from `internal-tool` as one isolated commit.
- Phase 3A is complete, and Phase 3B scheduling and automation remain paused.
- Personal scoring, alumni-specific behavior, and internal-only workflow changes are out
  of scope on this branch.
- Shared watcher and core fixes must arrive as reviewed, isolated commits.
- `hosted_user_job_matches` holds one durable row per `(user_id, job_id)`.
  Reconciliation never deletes history: losing a match stamps
  `no_longer_matches_at`, a later rematch clears it, and `saved_at`/
  `dismissed_at` are user-owned and survive every pass.
- Matching is pure and database-free in `hosted/matching.py`: watched and
  unpaused company, open job, selected hosted role, then location/remote and
  season compatibility. Never use watcher fit scores, LLM or fuzzy ranking,
  resumes, alert frequency, or the global notification pause.
- Reasons are allowlisted bounded codes with catalog identifiers only; never
  descriptions, raw preferences, or source metadata.
- Reconcile after job import (inside the import transaction), preference
  changes, and company-watch add/remove/pause/resume. `matches_created` counts
  only newly inserted rows; `already_imported` stays a strict no-op.
- Keep classification authoritative in the watcher. Hosted import must gate on
  the watcher internship/co-op predicate before role mapping and keep duplicate
  final watcher IDs structural failures.
- Preserve unaffected classifications and IDs, keep replay offline and
  watcher-state safe.
- Phase 3A enqueueing is import-only and shares the job/match transaction. It
  accepts only newly inserted matches for active, verified, unpaused users;
  preference/watch reconciliation, reactivation, and `already_imported` never
  create notification backlog.
- Durable batches/items/attempts live only in hosted PostgreSQL. As-detected
  groups by user/import; three-hour and daily use rolling first-item windows.
  The one-shot worker claims with `SKIP LOCKED`, token and 10-minute lease,
  commits before SMTP, retries safely at 1/5/15/60 minutes up to five attempts,
  and makes post-submission ambiguity terminal `uncertain`.
- Delivery revalidates users, frequency, matches, dismissals, and job openness.
  Use only hosted SMTP, resolve the verified address when sending, reuse the
  deterministic Message-ID, and never store/log addresses, bodies, raw SMTP
  errors, descriptions, source metadata, or watcher delivery state.

- After every user prompt, update the root `claude.md`, `agents.md`, and
  `.gitignore`. Keep them concise, synchronized, and relevant.
- Read `agents.md` before repository work. Before watcher work, also read
  `WATCHER_SPEC.md` in full; use `WATCHER_PROGRESS.md` only for current status.
- Change only the requested layer and preserve unrelated work.
- Route JSON endpoints through `_json_object`; malformed or excessively nested
  JSON and invalid request shapes are HTTP 400, never internal errors.
- Keep CSV cleanup in `process_csv`; use `analyze_rows` for canonical rows.
  Never duplicate backend scoring, classification, signals, dedupe, or IDs.
- Source adapters only fetch canonical rows. Eligibility belongs in
  `watcher/eligibility.py`; filters add internship/open/min-score checks.
- Student-status exclusions require clear mandatory evidence and use stable
  `phd_only`, `graduate_only`, `freshman_only`, or
  `returning_intern_only` reasons. Mixed, preferred, incidental, or ambiguous
  mentions remain eligible. Evaluate them only after internship/student-program
  and open checks; traces retain mandatory, negation, and mixed diagnostics.
- `Master data`, `master record`, `master dataset`, and `master schedule` are
  operational terms, not graduate-degree evidence; bare `master` needs explicit
  degree, program, student, or candidate context.
- `assess_us_location` is the sole location gate: explicit U.S. wins,
  explicit foreign country/region yields `outside_us`, and ambiguity passes.
  Prefer collected structured country/location metadata, keep city-only
  evidence ambiguous, preserve diagnostics, and never derive country from
  state abbreviations or alter scores/ranking.
- Role rules prioritize title/core duties over incidental keywords. Technical
  AI integration, digital solutions, quant, product, and umbrella programs
  require explicit central technical evidence; physical engineering, consumer
  research, and manufacturing quality remain excluded.
- Keep typed GitHub sources backward-compatible with `github_listing_urls`.
  Merge direct ATS, Simplify JSON, then Markdown by fixed priority; Markdown
  `Added` is source metadata and lower-priority closure cannot close direct data.
- Row provenance keys off `extra.source_adapter`, which `make_row` always sets.
  CSV `extra` is user data and never drives dedupe ordering or provenance.
- Track each GitHub source independently; valid feeds with zero matches succeed.
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
- Collection concurrency is opt-in and bounded. `WATCHER_COLLECTION_MODE`
  defaults to `serial`, serial stays the permanent rollback/diagnostic path, and
  only a separate reviewed change may promote `concurrent`. Validated limits are
  `WATCHER_COLLECTION_MAX_WORKERS` (1-16, default 4),
  `WATCHER_WORKDAY_MAX_CONCURRENCY` (1-5, default 1), and
  `WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY` (1-4, default 2); neither
  scope limit may exceed the worker pool and every applicable limit binds.
- Origin/provider keys use scheme, host, and port only; companies sharing an ATS
  host share one origin limit and Workday tenants share the Workday limit.
  Concurrency reorders nothing, never weakens pacing, timeouts, retries, or
  backoff, and adds no proxy, cookie, header-rotation, browser-automation, or
  challenge-bypass behavior. Blocked, unauthorized, and rate-limited responses
  stay ordinary source failures, and escaped worker errors become failed task
  results with sanitized exception types.
- Validate concurrency in stages: deterministic offline equivalence, a limited
  live canary across adapter types with at most one or two Workday tenants, then
  a full live canary. Canaries use temporary databases, disable email, priming,
  seen marking, health alerts, durable health persistence, and comparison
  persistence, stop testing blocked sources instead of retrying them, and never
  run full serial and concurrent collections back-to-back.
- Full live canaries record the local/remote SHA and worktree status, use fixed
  4/1/2 limits in separate normal collection windows, keep detailed reports
  private, and require identical production-SQLite fingerprints. Uncommitted
  or unpushed evidence is not repository-complete; missing normal GitHub
  authentication stops validation before canary, and production stays serial.
- Workday canary telemetry records monotonic tenant starts after pacing and
  immediately before fetch; only private reports receive sanitized identifiers,
  relative offsets, spacing statistics, and numeric violation counts.
- Canary summaries quote retained report fields exactly and label unavailable
  historical telemetry instead of reconstructing or estimating it.
- Persistent analysis caching is watcher-owned in a dedicated rebuildable
  SQLite database beside durable state; backend analysis stays pure and
  cache-independent, fingerprints are deterministic/versioned, and
  current-date scoring always runs for every deduplicated row.
- Cache corruption, schema mismatch, or SQLite failure is nonfatal and falls
  back to fresh analysis; batch reads, transactional writes, and one bounded
  30-day cleanup per run must preserve byte-identical jobs and dedupe reports.
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
- Dry runs never change notification state. Live sends populate `emailed_at`
  only after success; explicit priming has its own marker, and unmarked legacy
  rows remain pending.
- Scheduled send configuration distinguishes recognized true/false,
  missing/blank, and invalid values; disabled schedules warn with the pending
  count, while manual dry runs do not.
- Watcher audits are read-only and reuse production identity, dedupe,
  classification, eligibility, scoring, and seen lookup; state-only audits
  never fetch, and live audits never email, prime, or persist health attempts.
- Source comparison computes lightweight outcomes/counts for every job, then
  selects details before building and sanitizing rich traces. The report owns
  deterministic eligible/anomaly/non-routine/routine-sample retention; the
  store persists selected entries without a second policy pass. Decisive early
  reasons defer rich-only location/notification expansion until selection.
  Keep 30 aggregate runs and three detail runs. Health-alert
  cooldowns use dedicated tables and an independent email switch/renderer;
  they never update internship `emailed_at` or `primed_at`.
- Rollout verification disables internship email, priming, Workday probes, and
  health email; preserve notification timestamps and existing repository
  variables before enabling conservative health alerts.
- Collection and notification share one identity policy: stable requisition
  ID, posting-specific normalized URL, then exact company/title/location.
  Generic URLs never collapse distinct stable requisitions.
- Alumni data is additive and private.
- Ignore private/generated payloads, not reusable helper scripts or test
  fixtures; new code and fixtures must remain visible for review.
- Never commit `.env`, credentials, alumni data, SQLite state, probe/health
  output, downloaded or extracted Actions diagnostics, or
  `evaluation/private/`.
- Workday: log safe metadata only; retry only transient failures; never treat
  HTML as empty or use anti-bot evasion; never reset persistent state.
- Sanitizers are total: `sanitize_error`, `sanitize_feed_label`, and `_safe_url`
  run over arbitrary failure text and must never raise on a malformed URL.
- Tests and benchmark evaluation stay offline. Benchmarking must not alter
  scoring and must not use alumni, email, seen state, or workflow persistence.
- Frozen benchmarks retain international rows; current evaluation applies the
  production eligibility helper without regenerating frozen inputs.
- Never rewrite `scoring_20260724_*`. U.S. role-fit benchmarking uses the
  separate exporter and only `us`/`ambiguous` candidates with independent
  `random`, `likely_match`, and `difficult_negative` cohorts.
- Rebuild U.S. role-fit artifacts only from a clean commit, validate manifest
  hashes and `git_dirty=false`, and explain every change from the prior export.
- Benchmark labels require `human_eligible` (`yes`, `no`, or `uncertain`);
  optional role track, exclusion reason, and notes do not affect binary metrics.
- Use the validation commands in `agents.md`; always run `git diff --check`.
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
