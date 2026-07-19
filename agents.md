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

Current backend query layer:

- `backend/app/ask.py` is a deterministic query engine, not an LLM path.
- Backend-oriented Ask queries must not treat every broad SWE role as backend
  relevant. They should match backend-adjacent `role_track` values
  (`backend`, `full_stack`, `platform_infra`, `data_engineering`) or the
  existing `backend_focus` signal. Frontend-only or generic SWE roles without
  backend evidence should not appear in backend-specific Ask results.

Current backend API and override handling:

- JSON API endpoints must treat malformed JSON, non-object request bodies, and
  non-string `csv_text`/`question` fields as HTTP 400 client errors. Reuse
  `backend/app/main.py::_json_object` so parsing failures do not escape as 500s.
- Multipart ingestion must verify that the `file` form field is an uploaded
  file before reading it; a plain text form field named `file` is a 400.
- `KNOWN_COMPANIES_PATH` overrides are optional configuration. A valid override
  is a JSON object whose `tech`, `non_tech`, and `reputable` values are arrays;
  invalid top-level or per-list shapes fall back to the built-in values rather
  than crashing ingestion.

Current watcher fetch layer:

- Source adapters live under `watcher/sources/`.
- `watcher/sources/base.py` owns the `Source` protocol, source errors,
  fetch helpers, and `make_row`.
- `watcher/config.py` owns `CompanyCfg`, `WatcherConfig`, and the small
  `watchlist.yml` loader. The loader intentionally supports the simple
  top-level `defaults` + `companies` YAML shape used by this repo; there is no
  PyYAML dependency.
- Loaded production watchlists must explicitly define at least one nonblank
  `defaults.terms` value. Runtime dataclass construction has no implicit
  recruiting season. Company terms inherit from defaults when omitted, and an
  explicitly empty company override is invalid.
- Structured GitHub backstop URLs come from the inline
  `defaults.github_listing_urls` list. Values must be nonblank HTTP/HTTPS URLs;
  multiple feeds are supported and no recruiting-year feed URL belongs in
  Python source.
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
- Direct adapters isolate posting-level schema damage: a mixed payload retains
  valid records and emits one bounded aggregate warning, while a nonempty
  payload with zero valid records remains a source schema failure. Paginated
  adapters reject repeated pages instead of looping indefinitely.
- GitHub listing rows also retain safe feed provenance in `extra.feed_url`.
  Each configured feed is fetched once. A failed feed is identified in source
  errors without suppressing successful feeds or direct ATS rows.
- Adapter tests parse saved fixtures only; tests must never hit the network.
- For future adapter work, verify the live endpoint first, save representative
  real responses under `watcher/tests/fixtures/`, and parse fixtures in tests.
- Workday uses POST
  `https://{tenant}.{workday_shard}.myworkdayjobs.com/wday/cxs/{tenant}/{workday_site}/jobs`
  with JSON pagination. Capital One rejected limits above 20, so the adapter
  uses `limit: 20`. Some Workday URLs from the generated watchlist currently
  return Workday maintenance/refresh HTML instead of JSON; the adapter treats
  that as a fetch failure rather than a silent empty result.
- Workday skips isolated non-object postings and postings missing a usable
  title or `externalPath`, retaining valid postings from the same/later pages
  and logging one bounded aggregate warning with reason counts. Pagination uses
  raw posting count. Page-level schema errors and nonempty all-malformed fetches
  still fail; a valid zero-posting board remains a successful empty fetch.
- Workday transport classifies non-JSON responses from safe metadata only:
  status, query-free final URL, content type/encoding, bounded body length,
  generic body kind, and a SHA-256 digest. Raw bodies, cookies, sensitive
  headers, and challenge payloads must never be logged or persisted. The
  adapter retries only transient network/429/selected 5xx/empty/HTML failures,
  with three total attempts and bounded injectable backoff. Deterministic
  schema failures are not retried, and HTML is never treated as an empty board.
- Starting different Workday tenant fetches is paced by an instance-local
  pacer. `WATCHER_WORKDAY_MIN_INTERVAL_SECONDS` defaults to `0.5`, accepts
  values from `0` through `10`, and `0` disables pacing for controlled local
  diagnostics. Pagination pages for one tenant do not receive tenant pacing.
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
- The July 2026 alumni-company watchlist expansion added DoorDash, Tesla,
  ASML, HP, ZoomInfo, Intuitive Surgical, Whatnot, Augury, Goldman Sachs,
  JPMorgan Chase, Barclays, UBS, Nomura, BlackRock, AQR Capital, Federal
  Reserve Bank of New York, KPMG, and EY. Direct entries were added only where
  live public endpoints matched existing adapters; custom, unsupported, or
  unsafe-to-scope portals are marked `bespoke` with notes rather than
  fabricated adapter settings.
- The run loop skips `bespoke` and `github_only` entries for direct fetching,
  fetches direct rows first, then the GitHub backstop. This order is
  intentional: backend dedupe keeps the first duplicate row's `extra`, so
  direct rows win the source tag without changing backend dedupe.
- The run loop calls `backend.app.ingest.analyze_rows`; watcher code must not
  compute scores or ids itself.
- `collect_rows(..., direct_sources=None, github_source=None)` constructs the
  production adapters only for arguments that are actually `None`. When the
  GitHub source is not injected it builds one source per configured listings
  URL. An explicit empty `direct_sources={}` and an explicitly injected
  `github_source` are meaningful dependency injection; do not replace either
  through truthiness fallback.
- `watcher/season.py` owns deterministic season validation. `stale` means all
  recognized term years are in the past; `rollover_due` means July or later
  with only current-year terms; `ok` covers a future-year term or a current-year
  term before July; `unknown` means no four-digit year was found. Non-`ok`
  statuses warn but never stop direct ATS collection. Stale company-specific
  overrides must be named in warnings.
- Backend role classification now includes a narrow `role_track` plus
  `software_evidence` and `non_swe_evidence`. Generic `engineer` or
  `engineering intern` text must not imply SWE without strong software context.
  Electrical, mechanical, manufacturing, hardware/RF, civil/structural,
  factory automation, customer experience/support, commercial, and generic
  non-SWE engineering roles should be ineligible for the watcher unless clear
  software/backend/data/ML/platform evidence overrides the ambiguity.
- Backend scoring emits watcher-specific fields on `score`: `fit_score`,
  `watcher_eligible`, `watcher_ineligible_reason`, `fit_explanation`,
  `role_track`, `watcher_action`, and `watcher_action_label`. Watcher code uses
  these fields; it must not infer its own fit score.
- Backend scoring also emits degree-level watcher fields on `score` and on each
  analyzed job: `degree_level`, `degree_eligible`, and
  `degree_ineligible_reason`. Masters, PhD/doctoral, MBA, graduate-student,
  advanced-degree, and postdoc internships are outside the undergraduate target
  and must have `watcher_eligible=false`, `fit_score=0`, and digest exclusion
  even when the stack is otherwise a strong SWE match. Normal undergraduate,
  bachelor's/BS/BA, sophomore/junior/senior, college-student, or unspecified
  internships should not be excluded by the degree gate.
- `fit_score` is calibrated against the resume/profile, not role-track
  eligibility alone. A score of 100 should be rare and requires several direct
  resume-skill matches in a target track. Strongest resume skills include
  Python, Java, SQL, JavaScript/TypeScript, FastAPI, Flask, SQLAlchemy,
  Next.js, React, Pandas, OpenAI API, Git/GitHub, PostgreSQL, SQLite, REST,
  RESTful APIs, backend APIs, data ingestion, data analytics,
  spreadsheet/data apps, market/data pipelines, full-stack web apps, and
  testing/evals/Pytest. Rust, Go, C/C++, embedded/firmware, robotics hardware,
  CAD/mechanical, Kubernetes/Terraform/cloud ops, low-level distributed
  systems, SRE/DevOps, and mobile are weaker or missing profile matches.
- IT support, quality/test, and solutions engineering are deliberate
  low-priority exceptions: they may remain watcher-visible, but their
  `fit_score` should be capped around 20 unless a later task changes that
  policy.
- `watcher/filters.py` filters after scoring using watcher eligibility:
  `watcher_eligible=true`, positive `fit_score`, target role defaults to
  `swe`, internships/co-ops only, open/non-expired only, optional `min_score`
  default off and applied to `fit_score`.
- `watcher/seen_store.py` is the SQLite seen-store. It keys on the existing
  analyzed job `id`, which comes from `backend.app.dedupe.job_id`. A job seen
  via GitHub is not new later via direct, and vice versa.
- `watcher/alumni.py` loads a private compact company alumni JSON map first,
  then falls back to the private full alumni CSV. Loading priority is
  `WATCHER_COMPANY_ALUMNI_JSON_B64`, `WATCHER_COMPANY_ALUMNI_JSON`,
  `WATCHER_COMPANY_ALUMNI_JSON_PATH`, then `WATCHER_ALUMNI_CSV` or the default
  gitignored `watcher/alumni.csv`. The compact JSON shape is
  `{ "bosch": [{"name": "...", "occupation": "...", "linkedin_url": "...",
  "employer": "Bosch"}] }` and is converted into the same `AlumniIndex` shape
  used by CSV loading. Top-level JSON keys should be `norm_company`-normalized
  employer names, and records should contain only `name`, `occupation`,
  `linkedin_url`, and `employer`. Alumni data is additive only; it must never
  drop, reorder, gate, or rescore a posting.
- `scripts/build_watcher_alumni_map.py` builds the compact private JSON map
  from the full private CSV and `watcher/watchlist.yml`. It must write only
  alumni attached to watched companies and must not include unrelated alumni or
  extra private columns. Expected local command:
  `python scripts/build_watcher_alumni_map.py --csv "C:\path\to\alumni.csv" --watchlist watcher/watchlist.yml --out private/company_alumni.json`.
  The `private/` directory and common `*.private.*`/`*.secret.*` artifacts are
  ignored; never commit generated private alumni maps.
- Missing private alumni data must not be treated as a normal empty roster in
  live watcher mode. Set `WATCHER_REQUIRE_ALUMNI=1` when a missing or malformed
  private JSON/CSV source should hard-fail the run; live GitHub Actions sends do
  this.
- In GitHub Actions, the private compact JSON map is restored first from
  repository secret `WATCHER_COMPANY_ALUMNI_JSON_B64` into a temp file and
  exported as `WATCHER_COMPANY_ALUMNI_JSON_PATH`. The workflow then falls back
  to full CSV secrets including `WATCHER_ALUMNI_CSV_B64`, `ALUMNI_CSV_B64`,
  `WATCHER_ALUMNI_CSV_TEXT`, `WATCHER_ALUMNI_CSV`, `ALUMNI_CSV_TEXT`, and
  `ALUMNI_CSV`. If live email is requested and neither compact JSON nor CSV is
  available, the workflow fails before sending; dry runs continue with explicit
  warning text and alumni matching disabled.
- Alumni matching order is exact normalized employer match first, then
  hard-coded common aliases, then watchlist `aliases` and `alumni_match` values,
  with fuzzy matching only as a fallback. Keep private contact data out of the
  repo; use a local file, GitHub Actions secret/data branch, or another private
  loading path.
- `watcher/notify.py` renders one plain-text email digest for genuinely new
  matches. `render_digest(matches, alumni_summary=..., active_terms=...,
  season_status=...)` is pure and
  offline-tested. The digest header should include alumni index status such as
  `Alumni index: 124 records across 80 employers` or `Alumni index missing, no
  alumni matching was performed`. `send_digest` dry-runs to stdout unless
  `WATCHER_SEND_EMAIL` is truthy; live Gmail SMTP requires `SMTP_USER`,
  `SMTP_APP_PASSWORD`, and `EMAIL_TO` from env.
- The run loop marks jobs seen only after `send_digest` reports a successful
  live send by default. Dry-run digest previews do not advance the seen-store
  unless `python -m watcher.run --mark-seen-without-send` is used for the
  explicit GitHub Actions priming flow.
- Digest decisions are settled: no score gate, exclude watcher-ineligible jobs,
  sort by `fit_score` descending, then generic score, role-track priority, and
  company/title tie-breaks, and send nothing when there are zero new matches.
  The digest should show score, fit score, role track, fit reason,
  recommendation, red flags, apply URL, source tag, alumni index summary, and
  alumni annotations. Do not print an unqualified `No alumni on file` fallback:
  distinguish `Alumni matching disabled; roster not loaded` from `No matching
  alumni in loaded roster`. Graduate-level excluded roles may appear in debug
  output but must never appear in the email digest.
- Local seen-store files are ignored by `.gitignore`; pass `--seen-db` in tests
  or manual runs when you want an isolated store. The default seen-store path is
  `watcher/seen.sqlite`, configurable with `WATCHER_SEEN_DB`.
- `.github/workflows/watcher.yml` runs the watcher hourly and by manual
  dispatch. It restores `seen.sqlite` from the orphan `watcher-data` branch into
  the path named by `WATCHER_SEEN_DB`, runs `python -m watcher.run`, then commits
  and pushes the DB back to `watcher-data`.
- Workflow dispatch input `send_email=false` is the priming path: it exports
  `WATCHER_SEND_EMAIL=0`, suppresses the digest body so private alumni details
  do not enter Actions logs, marks the new matches seen with
  `--mark-seen-without-send`, and persists that DB so the first later send does
  not email the whole backlog. Scheduled runs read the repo Actions variable
  `WATCHER_SEND_EMAIL`; live sends require the repo secrets `SMTP_USER`,
  `SMTP_APP_PASSWORD`, and `EMAIL_TO`.
- The watcher prints a run heartbeat. The workflow captures its exact last
  one-line `HEARTBEAT:` output, forwards every current/future application field,
  and appends only `seen_loaded`, `seen_saved`, and `seen_store`. A missing
  application heartbeat is an explicit error. The heartbeat includes run
  counts, source-error count, alumni index status
  (`alumni_csv_status=loaded-json-map/loaded-csv/missing/empty/error`,
  `alumni_records_loaded=<n>`, `alumni_employers_indexed=<n>`), seen-store
  load/save counts, send result, and persistence status. Source adapter
  failures are logged and surfaced as warnings; seen-store load corruption or
  push failure is a hard workflow failure.
- Both heartbeats preserve `season_status`, heartbeat-safe `configured_terms`,
  `github_feeds_configured`, and `github_feeds_succeeded`. Multiple terms use
  `|` separators and underscores for spaces; heartbeat values must not add raw
  commas.
- Workday heartbeat fields are integer-only: `workday_attempted`,
  `workday_succeeded`, `workday_failed`, `workday_retry_attempts`, and
  `workday_shared_incident`. A likely shared incident requires at least five
  failed Workday tenants and one supported transient transport classification
  accounting for at least 60% of the Workday failures. It adds an aggregate log,
  report, Actions warning, and JSON summary without suppressing per-company
  health attempts or resetting counters.
- Manual dispatch input `workday_transport_probe=true` runs only the isolated
  five-tenant safe probe. It sets `WATCHER_SEND_EMAIL=0`, uses no seen database,
  alumni data, or SMTP secret, never writes `watcher-data`, and emits only safe
  transport metadata. Do not add cookies, challenge tokens, browser automation,
  proxy rotation, or other anti-bot evasion.

Current watcher source-health layer:

- `watcher/source_health.py` owns source-attempt/state dataclasses, pure status
  and transition rules, effective coverage, stable health keys, sanitization,
  SQLite health persistence, summaries, JSON output, and Actions rendering. It
  must never make network requests.
- Every run uses one run ID/UTC observation time and records exactly one direct
  outcome per configured company plus one outcome per configured GitHub feed.
  Direct collection remains before GitHub collection.
- Direct status thresholds are: nonzero success `healthy`; zero success
  `empty`; repeated zero successes become `degraded` only after a prior nonzero
  success; one/two consecutive failures `degraded`; three or more `failing`;
  `bespoke`/`github_only` `unsupported` without failure increments. GitHub valid
  payloads are `healthy` even with zero matching rows and use only the
  one/two-degraded, three-failing failure thresholds.
- Initialization is not an actionable transition. Unchanged states do not
  re-alert. `degraded`/`failing` to `healthy`, or to a responding direct
  `empty`, is a recovery.
- Company coverage is operational, not opening availability. Successful zero
  direct results are `direct_empty_but_responding`; a successful configured
  GitHub feed is an available backstop even without a company posting;
  `uncovered_for_run` requires a failed/unsupported direct source and no
  successful configured feed.
- `source_health_attempts` and `source_health_current` live in the existing
  `seen.sqlite`. Schema creation must not alter `seen`; attempt insertion and
  current-state upsert are one transaction. Deleting `watcher-data` resets both
  seen and health histories and must not produce false recovery transitions.
- Health error messages and feed labels are bounded/sanitized. Raw query
  strings and credentials must not enter keys, heartbeats, annotations, or
  stored errors.
- Health appears in logs, the normal report, application/final heartbeats, the
  sanitized `WATCHER_HEALTH_REPORT_PATH` JSON, Actions summary, transition
  warnings, and uncovered annotations. These states remain nonfatal and do not
  change email or seen-marking behavior. No health-warning email exists yet.
- Source-health tests are offline with fake adapters, fixed times/run IDs, and
  temporary SQLite files.

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
PYTHONPATH=.:backend python3 -m compileall -q backend watcher scripts
```

For frontend changes:

```bash
cd frontend
npm test
npm run build
```

When WSL's Linux Node runtime fails inside Vite/Vitest while reading this repo
from `/mnt/c`, use the installed Windows Node runtime instead:

```bash
cmd.exe /C "cd /D C:\Users\burst\internship-signal\frontend && npm test -- --run && npm run build"
```

As of the July 2026 audit, `npm audit --omit=dev` is clean and the non-breaking
`form-data` fix is locked at 4.0.6. The remaining audit findings are in the
Vite/Vitest development toolchain and require major-version upgrades. Do not
use `npm audit fix --force` as a drive-by fix; treat that as an explicit
dependency-upgrade task and rerun frontend tests/build under the supported Node
runtime.

To run the watcher once from the repo root:

```bash
PYTHONPATH=.:backend python3 -m watcher.run --seen-db /tmp/internship_signal_watcher.sqlite
```
