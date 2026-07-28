# Agents Guide

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

- Use `backend/app/main.py::_json_object` for JSON endpoints. Malformed JSON,
  non-object bodies, and non-string `csv_text`/`question` values are HTTP 400.
- Multipart `file` must be an upload. CSV input is limited to 10 MiB; oversized
  input is HTTP 413.
- Keep frontend CSV formula-injection protection.
- Invalid optional `KNOWN_COMPANIES_PATH` data falls back to built-ins.

## Watcher config and sources

- `watcher/config.py` owns the small dependency-free watchlist loader and
  dotenv loading; process environment values win.
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
- Supported ATS values are `greenhouse`, `lever`, `ashby`,
  `smartrecruiters`, `workable`, `workday`, `bespoke`, and `github_only`.
  Workday requires tenant, shard, and site.
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

- `watcher/run.py` fetches direct sources before GitHub so backend dedupe keeps
  direct provenance. `bespoke` and `github_only` skip direct fetching.
- In `collect_rows`, only `None` means “construct defaults.” Preserve explicit
  empty injected sources.
- Watcher code must not compute scores or IDs. Backend scoring owns
  `fit_score`, role track, eligibility, explanations, actions, and degree
  decisions.
- `watcher/eligibility.py` is the only watcher-side target-role/degree wrapper;
  `watcher/filters.py` then requires eligible, positive-fit, open
  internships/co-ops and applies optional `min_score`.
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

## Alumni and private data

- Alumni data is additive only: never gate, reorder, or rescore jobs.
- Loading priority is compact JSON env/text/path, then CSV env/path. Live sends
  require usable alumni data; dry runs may report matching disabled.
- Matching order is normalized exact, built-in aliases, watchlist
  aliases/`alumni_match`, then conservative fuzzy fallback.
- Keep `.env`, `private/`, alumni files, SQLite state, health/probe reports, and
  `evaluation/private/` out of Git. Never log private contacts or SMTP
  recipients.

## Source health and Workday

- `watcher/source_health.py` is pure and makes no network requests. Record one
  direct outcome per company and one outcome per GitHub feed per run.
- Health state shares `seen.sqlite`; writes are transactional and must not alter
  `seen`. Initialization is not a transition. Health is nonfatal and does not
  affect email or seen marking.
- Track every configured GitHub source independently; a valid parsed feed is
  healthy even when watchlist filtering yields no matches.
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
- Preserve the application's exact final `HEARTBEAT:` and append only
  `seen_loaded`, `seen_saved`, and `seen_store`. A missing heartbeat or corrupt
  seen-store is fatal; source failures remain warnings.
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
PYTHONPATH=.:backend python3 -m compileall -q backend watcher scripts
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
- Holdout construction is two-stage: commit reusable tooling first, then
  collect from that exact clean SHA. Exclude both prior benchmarks by stable
  ID, normalized URL, and fallback key without reading their human labels.
