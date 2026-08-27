# Internship Signal

Separate real engineering internships from busywork. Paste or upload a messy CSV of
postings; get back a cleaned, deduplicated, scored, and flagged board with a
plain-English explanation for every number.

Built for a CS student profile (backend/data/ML-leaning, Flask + SQLAlchemy
experience, Cornell, prefers paid roles with real ownership) — the profile is a
JSON file you can edit, not a hardcoded assumption.

Everything runs locally. No external APIs, no LLM calls, no telemetry.

---

## Quickstart

Requirements: Python 3.10+ and Node 18+.

**1. Backend (FastAPI, port 8000)**

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**2. Frontend (Vite + React, port 5173)**

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 and click **load the sample dataset** (or drop in your
own CSV). The Vite dev server proxies `/api/*` to `localhost:8000`, so there is no
CORS or URL configuration in normal use. `.env.example` documents the few
overridable settings.

For persistent multi-user accounts, HTTP-only sessions, preferences, and
per-user watchlists, follow [backend/HOSTED_BACKEND.md](backend/HOSTED_BACKEND.md).

**Run the tests**

```bash
cd backend && python3 -m pytest tests/ -q     # 86 passed
cd frontend && npm test                        # 20 passed (vitest)
```

Actual output from this machine:

```
backend:  86 passed, 1 warning in 0.61s
frontend: Test Files  3 passed (3)
          Tests  20 passed (20)
```

(The one warning is a Starlette deprecation notice from FastAPI's TestClient,
unrelated to app code.)

---

## Watcher GitHub backstops, season configuration, and rollover

The recruiting cycle and typed GitHub backstops are explicit in
`watcher/watchlist.yml`:

```yaml
defaults:
  terms: ["Summer 2027"]
  github_listing_sources:
    - name: simplify
      format: simplify_json
      url: "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json"
    - name: sndsh404_summer_2027
      format: github_markdown_table
      url: "https://raw.githubusercontent.com/sndsh404/summer-2027-internships/main/README.md"
      default_term: "Summer 2027"
```

`defaults.terms` is required and cannot be blank. Companies inherit it unless
they provide a nonempty `terms` override. `simplify_json` applies the exact
structured term values supplied by Simplify. `github_markdown_table` assigns
its required `default_term`, then applies the same company term filter; adding
another feed in either supported format requires only another configuration
entry. The legacy `github_listing_urls` list remains supported and is
interpreted as one or more `simplify_json` sources.

Every configured feed is fetched and health-tracked independently. A valid
payload/table is healthy even if it yields zero watchlist matches. Transport,
schema, missing-table, and all-malformed-table failures affect only that source;
other GitHub sources, direct ATS results, digest generation, and seen-state
handling continue.

Rows are merged before scoring and notification with fixed precedence,
independent of source configuration order:

1. direct company ATS;
2. Simplify structured JSON;
3. configured GitHub Markdown tables.

Posting identity is shared by collection dedupe and notification suppression.
It uses, in order:

1. a stable source-native ATS requisition/posting ID;
2. a posting-specific normalized application URL;
3. exact normalized company/title/location when neither stronger key exists.

Tracking parameters and harmless URL formatting differences are removed.
Careers landing pages, search results, generic internship/program pages, and
URLs shared by multiple distinct source-native IDs are not treated as
posting-specific. Different stable requisition IDs always remain separate,
even when their titles, locations, role tracks, or generic careers URLs match.
The analyzed backend `job["id"]` remains the existing content hash and is not
treated as an ATS requisition ID.

For a genuine duplicate, the winner keeps its canonical fields, missing fields
may be filled from lower-priority rows, and provenance records
`primary_source`, every discovering source in `sources`, plus per-source
details. A lower-priority closed marker never closes an active direct or
Simplify result. A direct ATS and GitHub sighting of the same requisition still
produce one notification, with direct provenance winning.

### Watcher notification modes

The watcher has three explicit modes:

- **Live send:** set `WATCHER_SEND_EMAIL=1` (or manual `send_email=true`).
  Current pending matches are emailed, then and only then written with
  `emailed_at`. SMTP failure leaves every posting pending.
- **Dry run:** set `WATCHER_SEND_EMAIL=0`, leave `prime_seen=false`, and do not
  pass `--prime-seen`. Matches are previewed/reported and no rows are inserted
  or updated in the `seen` notification table.
- **Explicit prime:** keep email disabled and pass `--prime-seen` (or manual
  `prime_seen=true`). Current pending matches are intentionally suppressed with
  `primed_at`; email transport is not invoked.

`send_email=true` and `prime_seen=true` are rejected as incompatible. Scheduled
runs read `WATCHER_SEND_EMAIL` and the separate optional repository variable
`WATCHER_PRIME_SEEN`; an email-disabled schedule is an ordinary side-effect-free
notification dry run unless that second variable is explicitly enabled.

Scheduled send-mode parsing accepts `1`, `true`, `yes`, `y`, and `on` as true,
and `0`, `false`, `no`, `n`, and `off` as false, ignoring case and surrounding
whitespace. A missing or blank `WATCHER_SEND_EMAIL` is reported separately from
an explicit false value. Any other nonblank value is invalid, emits an Actions
warning naming the variable, and resolves conservatively to false.

Every normal Actions run starts its job summary with **Scheduled delivery**.
Scheduled runs report whether delivery is enabled and how many otherwise-new
postings remain pending because it is disabled. A disabled schedule emits a
nonfatal warning even when the pending count is zero; manual dry runs show
`not applicable` and do not receive that warning. The final workflow heartbeat
preserves the complete application heartbeat and adds
`scheduled_email_enabled`, `pending_due_to_email_disabled`, and
`scheduled_email_config` before the existing seen-store persistence fields.

Older SQLite files migrate in place with nullable `primed_at`,
`analyzed_job_id`, `identity_key`, `requisition_key`, and `location` columns.
The table is neither deleted nor rebuilt. Legacy rows whose `emailed_at` is
blank and which have no `primed_at` remain pending, so still-open eligible jobs
can be included in the next successful live digest.

The Markdown parser finds the table by its
`Company | Role | Location | Apply | Added` headers, extracts Markdown apply
links, and records `🔒` (closed), `🛂` (no sponsorship), and `🇺🇸` (US
citizenship required) before removing the markers from normalized display text.
Closed Markdown-only rows are scored but excluded from notifications. `Added`
is stored as `extra.source_added_date`; it is deliberately never copied to
`date_posted` or used as an employer posting date for freshness scoring.

The Simplify URL was live-verified on July 15, 2026. Its repository name is
historical, but its payload includes the exact `Summer 2027` term. The
`sndsh404` README table was live-verified on July 24, 2026. Both remain
configuration values and must be rechecked during future rollovers.

The run reports a deterministic season status from four-digit years in the
default terms:

- `ok`: a future-year term exists, or it is before July and a current-year term
  exists.
- `rollover_due`: it is July or later and the newest configured year is the
  current year.
- `stale`: every recognized configured year is in the past.
- `unknown`: no four-digit year can be extracted.

Non-`ok` statuses warn but do not stop collection. The report, digest, and
heartbeat expose the active terms, season status, and configured/successful
GitHub feed counts.

To roll from Summer 2027 to Summer 2028 without editing Python:

1. Find the active repository on the official SimplifyJobs GitHub organization;
   do not infer a repository name from the year.
2. GET the candidate raw `listings.json` URL and confirm HTTP 200, a top-level
   list, and every entry's required keys: `company_name`, `title`, `locations`,
   `url`, `date_posted`, `active`, and `terms`. Confirm `locations` and `terms`
   remain lists and inspect the exact term strings.
3. Verify each configured Markdown URL still returns UTF-8 Markdown with the
   expected five-column internship table. Update its URL and `name` if the
   repository changes, and set `default_term` to the new exact term.
4. Change `defaults.terms` to `["Summer 2028"]`. Keep only feeds verified to
   contain the intended cycle; overlapping feeds may coexist as separate typed
   entries.
5. Run the offline backend/watcher tests, then run a separate isolated probe:

```bash
probe_db="$(mktemp --suffix=.sqlite)"
probe_report="$(mktemp --suffix=.json)"
WATCHER_SEND_EMAIL=0 PYTHONPATH=.:backend python3 -m watcher.run \
  --seen-db "$probe_db" \
  --health-report "$probe_report"
```

Do not add `--prime-seen` to the probe. Tests never access the
network: adapter tests use saved UTF-8 fixtures and run-loop tests use mocks.
Use an explicit false value rather than unsetting `WATCHER_SEND_EMAIL`, because
the repository dotenv loader may otherwise restore a local `.env` send setting.
Confirm the report contains one independent successful attempt for every
configured feed, `sent=no`, `seen_marked=0`, and zero rows in the temporary
database's `seen` table. Live endpoint verification is deliberately separate
from fixture-based tests. If no verified next-cycle feed exists for a format,
remove only that typed entry and rely on the remaining sources.

The Workday adapter skips isolated malformed posting records while retaining
valid records from the same and later pages. It logs one bounded aggregate
warning with stable reason counts and never logs raw postings. Structurally
invalid pages still fail, as does any nonempty fetch that produces zero valid
canonical rows; a genuinely empty Workday board remains a successful empty
source.

iCIMS entries use `ats: icims` with an explicit `icims_variant` of
`jibe_json` or `classic` and a hostname-only `icims_host`. Multi-portal classic
sources also list the complete ordered `icims_portals`; every portal must
enumerate successfully. The adapter uses anonymous Jibe JSON or the classic
iframe listing response, never the outer shell or per-job enrichment.

The Simplify JSON backstop likewise retains valid entries from a mixed
malformed payload and emits one bounded warning. Its deliberately nonempty feed
still fails when every entry is malformed.

Workday transport failures are diagnosed without retaining response bodies.
Safe diagnostics include HTTP status, query-free final URL, content metadata,
body byte count and SHA-256 digest, a generic body classification, attempt
number, and retryability. Raw HTML, cookies, sensitive headers, and challenge
values are neither logged nor stored. HTML and empty responses remain fetch
failures—not empty job boards.

The adapter makes at most three attempts for transient failures: HTTP 429,
500/502/503/504, timeouts, temporary DNS/connection failures, empty responses,
and potentially transient HTML/non-JSON responses. HTTP 400/401/404 and plain
403 responses fail immediately, as do valid-JSON schema errors. Retry delays
are bounded (about 1–2 seconds, then 3–5 seconds); `Retry-After` is honored for
429 responses with a 10-second cap. Starting a different Workday tenant is
paced by `WATCHER_WORKDAY_MIN_INTERVAL_SECONDS`, which defaults to `0.5`, must
be from `0` through `10`, and may be set to `0` to disable pacing locally.
Pagination within one tenant does not incur that tenant-level delay.

When at least five Workday tenants fail and one stable transient transport
classification accounts for at least 60% of those failures, the run reports a
likely shared incident. This adds aggregate logs, JSON/report detail, an Actions
warning, and integer heartbeat fields (`workday_attempted`,
`workday_succeeded`, `workday_failed`, `workday_retry_attempts`, and
`workday_shared_incident`). Every company still records its own failed health
attempt; no counter is reset or source declared healthy. A later success creates
the normal one-time recovery transition.

Run the isolated five-tenant comparison probe without a seen database, alumni
data, or email:

```powershell
$env:WATCHER_SEND_EMAIL = "0"
$env:PYTHONPATH = ".;backend"
backend\venv\Scripts\python.exe scripts\probe_workday_transport.py
```

The `workday_transport_probe=true` manual workflow-dispatch input runs the same
safe probe on a GitHub-hosted runner without restoring or saving
`watcher-data`. It prints only company/shard, attempt count, status, content
metadata, body size/hash prefix, JSON decode state, and jobs-field presence. If
GitHub-hosted runners remain blocked, keep the GitHub backstop active and
investigate legitimate Workday access with the provider; this project does not
harvest cookies, rotate proxies, automate browsers, or bypass challenges.

### Watcher timing logs

Normal watcher runs emit stable INFO records measured with
`time.perf_counter()`. Each attempted direct ATS or GitHub backstop fetch emits
one `SOURCE-TIMING` line with a sanitized company/adapter identifier, success,
three-decimal elapsed seconds, and returned-row count. GitHub feed records use
`company=all` plus the configured source name. Adapters that expose request and
retry diagnostics, currently Workday, add `requests` and `retries`.

The run also emits `STAGE-TIMING` records for configuration/startup, direct and
GitHub collection, total collection, health persistence, analysis,
filtering/eligibility, alumni work, seen partitioning, digest/email handling,
source-comparison work, health-alert evaluation, and total runtime. Timing
records are emitted from `finally` blocks, including failed fetches, and never
contain feed URLs, response content, secrets, alumni details, or recipients.
They remain log-only so the existing heartbeat and health-report schemas stay
unchanged.

### Bounded collection concurrency

After three successful full canaries, the scheduled production workflow uses
concurrent collection with four global workers, one Workday task, and two tasks
per origin (`4/1/2`). The application default remains `serial` for local runs
and as the rollback path. Rollback requires changing only
`WATCHER_COLLECTION_MODE` in the workflow to `serial`.

| Setting | Default | Accepted range |
| --- | --- | --- |
| `WATCHER_COLLECTION_MODE` | `serial` | `serial`, `concurrent` |
| `WATCHER_COLLECTION_MAX_WORKERS` | `4` | 1–16 |
| `WATCHER_WORKDAY_MAX_CONCURRENCY` | `1` | 1–5 |
| `WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY` | `2` | 1–4 |

Workday and per-origin concurrency may never exceed the global worker pool, and
invalid values fail configuration loudly. A task starts only when *every*
applicable limit allows it: the global pool, its origin, its provider, and — for
Workday — the Workday limit. The tightest bound wins. Origin and provider keys
are derived from scheme, host, and port only, so no credential, path, or query
value can enter a key, and two different companies on the same ATS host share
one origin limit. Each Workday tenant is its own host but still shares the
single Workday provider limit.

Concurrency changes only *when* the existing fetch callables run. Adapter
timeouts, tenant pacing, retries, backoff, and source-specific safety rules are
unchanged; the Workday tenant pacer is shared across worker threads so pacing
cannot be weakened. There is no proxy rotation, cookie handling, header or
identity rotation, browser automation, challenge or CAPTCHA handling, retrying
through alternate infrastructure, or use of undocumented endpoints. Challenge,
forbidden, unauthorized, and rate-limited responses remain ordinary source
failures. Results are reassembled in configuration order, so rows, errors,
attempts, counters, dedupe precedence, and downstream output are identical to
serial collection.

Runs log one `COLLECTION-CONCURRENCY` record and add
`collection_mode`, `collection_max_workers`,
`collection_max_observed_concurrency`,
`collection_max_observed_origin_concurrency`,
`collection_max_observed_workday_concurrency`, and
`collection_unexpected_task_exceptions` to the heartbeat. Snapshot replay
reports `collection_mode=none`.

Full canary reports also include Workday tenant-start telemetry measured with a
monotonic clock at the boundary between pacing completion and adapter fetch.
The private report records the configured interval, start count,
minimum/median/maximum spacing, pacing-violation count, and sanitized company/
task identifiers with offsets from the first start. The pacer sleeps without
holding its coordination lock. These fields are in-memory canary evidence only:
they never enter snapshots, SQLite, durable health data, heartbeats, or email.

Staged validation:

```bash
# Stage 1 — deterministic offline benchmark (no network, no state)
PYTHONPATH=.:backend python3 scripts/benchmark_collection_concurrency.py \
    --companies 40 --delay 0.05 --output evaluation/private/stage1.json

# Stage 2 — limited live canary (small allowlist, at most one or two Workday tenants)
PYTHONPATH=.:backend python3 scripts/canary_collection_concurrency.py \
    --stage limited --max-sources 6 --max-workday 1 \
    --output evaluation/private/canary-limited.json

# Stage 3 — full live canary, only after the limited canary passes
PYTHONPATH=.:backend python3 scripts/canary_collection_concurrency.py \
    --stage full --output evaluation/private/canary-full.json
```

Before a full canary, record `git status --short --branch`, local `HEAD`, the
upstream ref, a fresh remote branch tip, and the watchlist SHA-256. The
concurrency implementation and canary tooling should already be committed and
pushed on the intended branch. If they are uncommitted or unpushed, report the
exact state and do not call the evidence repository-complete; do not commit or
push without separate authorization.

Canaries are collection-only and operationally isolated: temporary seen and
analysis-cache databases, email disabled, priming disabled, no seen marking, no
health-alert delivery, no durable health persistence, and no source-comparison
persistence. Production SQLite state is fingerprinted before and after and the
result is reported. A source answering 401, 403, 429, a challenge response, or
repeated transport failures is recorded and dropped from the rest of the canary
rather than retried. Full runs are guarded so at least one normal collection
interval separates them, and a full serial collection is never run back-to-back
with a concurrent canary — use a recent normal serial production run, existing
serial timing logs, or a serial run from a separate normal collection window as
the baseline.

Use fixed limits of four global workers, one Workday tenant, and two tasks per
origin for every promotion canary. Store detailed reports and any snapshots
only in `evaluation/private/`. A promotion report must distinguish historical
evidence from canaries attributable to a committed and pushed implementation,
and must record complete run identity, timing, concurrency, per-source,
Workday, downstream, and production-state safety fields.

Promotion to concurrent scheduled collection is a separate, small, reversible
change and requires the evidence listed in `WATCHER_PROGRESS.md`: at least three
successful full concurrent canaries in separate normal collection windows, no
material source-failure or Workday-retry increase, no new rate-limit or
challenge behavior, comparable per-source row coverage, complete source-attempt
diagnostics, identical deterministic fixture outputs, stable ordering and
deduplication precedence, clean executor shutdown, and no operational-state side
effects.

### Analysis performance benchmark

`scripts/benchmark_analysis_context.py` exercises the normal offline
`analyze_rows()` path at 500, 1,000, and 2,000 rows. It first compares the
context-enabled and context-free paths as deterministic JSON and fails
if they differ. By default it reads the ignored U.S. role-fit row export; pass
`--input` to use another representative canonical-row JSONL file.

```bash
PYTHONPATH=.:backend python3 scripts/benchmark_analysis_context.py
```

Against the recorded Windows baseline, the shared posting-analysis context
measured 6.555s versus 7.400s at 500 rows (11.4% faster), 13.004s versus
14.658s at 1,000 rows (11.3% faster), and 26.244s versus 29.307s at 2,000 rows
(10.5% faster). The fixed 74-row equivalence corpus produced identical
serialized jobs and dedupe reports.

### Persistent analysis cache

The watcher caches only date-independent backend analysis artifacts in the
`analysis_cache` table of a dedicated, rebuildable `analysis-cache.sqlite`
database. Durable `seen.sqlite` remains limited to notification, source-health,
health-alert, and source-comparison state. The backend CSV/API path remains
cache-independent. Every watcher run still deduplicates current rows first and
fingerprints their static inputs. Artifacts include the categorical
student-eligibility decision, reusable qualification parsing, and the seven
non-deadline scoring category results. Every run still recomputes deadline
urgency and expiration, reconstructs the category mapping and final weighted
score, reapplies caps/actions, generates reasons and concerns, assembles
current job IDs and row fields, and sorts every job. Current `extra` provenance
and other final row fields therefore never come from the cache.

`WATCHER_ANALYSIS_CACHE_ENABLED` defaults to `true` and accepts
`true`/`false`, `yes`/`no`, `on`/`off`, or `1`/`0`.
`WATCHER_ANALYSIS_CACHE_PATH` overrides the cache path; otherwise it is
`analysis-cache.sqlite` beside the configured seen database. Cache reads are
batched, new artifacts are written in one transaction, and entries not
accessed for 30 days are removed at most once per run. A missing, corrupt, or
unavailable cache produces a warning and falls back to fresh analysis without
opening a transaction against durable state.

Legacy cache rows can be copied explicitly without changing the source:

```bash
PYTHONPATH=.:backend python3 scripts/migrate_analysis_cache.py \
  --source seen.sqlite \
  --destination analysis-cache.sqlite
```

Add `--remove-source-table` only for an intentional cleanup. That mode creates
and validates a backup, drops only `analysis_cache` and its indexes, vacuums the
source, and revalidates all non-cache tables. GitHub Actions uses a daily,
versioned `actions/cache@v4` key for `analysis-cache.sqlite`; only
`seen.sqlite` is committed to the `watcher-data` branch.

`watcher.analysis_cache.STATIC_ANALYSIS_CACHE_VERSION` is part of every
SHA-256 fingerprint. Increment it whenever static classification,
compensation parsing, signal detection, profile matching, technology
detection, student eligibility, static category scoring, fingerprint inputs,
or the cached-artifact schema changes. The fingerprint includes canonical
location/remote fields and only structured `extra` values actually consumed by
student eligibility. Source provenance, observations, request/retry counts,
fetch timestamps, health metadata, deadline dates, and dynamic watcher target
roles are deliberately excluded.

Each run emits one safe summary without cache keys or posting contents:

```text
ANALYSIS-CACHE enabled=true rows=11897 hits=11000 misses=897 invalid=0 writes=897 hit_rate=0.924 lookup_seconds=0.100 static_analysis_seconds=18.000 scoring_seconds=4.000
```

The offline row benchmark uses identical canonical rows and a temporary SQLite
file:

```bash
PYTHONPATH=.:backend python3 scripts/benchmark_analysis_cache.py --rows 2000
```

The production-sized replay benchmark also measures the complete downstream
pipeline without collection:

```bash
PYTHONPATH=.:backend python3 scripts/benchmark_static_scoring_cache.py
```

On the fixed 11,855-job snapshot, disabled caching took 204.310s total and an
empty cache took 209.714s. The warm cache took 28.224s with 11,855 hits and no
misses. Dynamic scoring/final assembly fell from the previous 42.883s baseline
to 0.909s (97.9%); total replay fell from 63.464s to 28.224s (55.5%).
Source-comparison code was intentionally unchanged and took 20.308s in that
isolated run. The expanded artifact database was 123,641,856 bytes, 33,984,512
bytes larger than the prior artifact database. Disabled, cold, and warm jobs,
dedupe reports, matches, and in-memory comparison output had the same
deterministic SHA-256, and replay left operational state unchanged.

### Collection snapshot and replay

Internal watcher runs can capture live collection once, then replay the exact
canonical rows and collection diagnostics without requesting ATS or GitHub
endpoints again:

```bash
PYTHONPATH=.:backend python3 -m watcher.run \
  --capture-collection-snapshot watcher/collection-snapshots/latest.json.gz \
  --seen-db .watcher-state/seen.sqlite

PYTHONPATH=.:backend python3 -m watcher.run \
  --replay-collection-snapshot watcher/collection-snapshots/latest.json.gz \
  --seen-db .watcher-state/seen.sqlite
```

Snapshots are versioned, strictly validated gzip JSON written through atomic
temporary-file replacement. Sorted compact JSON and a zero-mtime,
filename-free gzip stream make identical batches byte deterministic. Schema v2
includes aggregate Workday request/retry and tenant outcome diagnostics. They
contain complete posting text and collection diagnostics, so `*.json.gz` and
the default `watcher/collection-snapshots/` directory are ignored by Git. Do
not publish them as routine CI artifacts.

Capture mode performs ordinary live collection and then continues normally.
Replay replaces only collection input: it uses the same deduplication,
analysis/cache, scoring, filtering, alumni, seen partition, and in-memory
source-comparison code. Replay is permanently dry-run and never calls network
sources, sends either email type, marks or primes postings, records source
health, writes a health report, or persists source comparison. The
static-analysis cache remains enabled normally because it accelerates current
analysis rather than recording a replayed collection observation.

The snapshot fingerprint covers company order/names/aliases, ATS types and
identifiers, collection terms, and ordered typed GitHub sources. Scoring,
profile, known-company, filtering, alumni, email, seen-store, and cache
settings are deliberately excluded. A mismatch fails before analysis unless
`--allow-collection-config-mismatch` is explicitly passed.

Replay uses the snapshot's captured UTC date for date-relative scoring.
`--today YYYY-MM-DD` provides an explicit deterministic override for tests.
Capture and replay options are mutually exclusive. Each operation emits one
safe `COLLECTION-SNAPSHOT` summary without posting content or feed URLs.

The isolated live/disabled-replay/warm-replay benchmark writes operational
state only to temporary databases:

```bash
PYTHONPATH=.:backend python3 scripts/benchmark_collection_replay.py
```

The isolated July 30, 2026 schema-v2 run captured 11,858 rows in a
5,689,148-byte compressed snapshot. Live dry capture spent 208.299s collecting
and 158.718s in static analysis plus scoring, with 388.333s total runtime.
Replay with caching disabled took 179.099s total and 159.124s analysis.
Warm-cache replay took 77.151s total and 52.365s analysis with 11,855 hits,
zero misses, and a 100% hit rate—a 56.9% total-time improvement over
disabled-cache replay. Deterministic jobs, dedupe reports, matches, and
normalized in-memory source comparison were identical. Both replay legs
skipped collection, left operational SQLite state unchanged, and sent no
notifications or health alerts.

Network availability was partial during that isolated run: six of 26 Workday
tenants succeeded and 20 exhausted normal retries with `network_failure`; the
snapshot retained 155 request attempts and 40 retries.
Treat the live total as a degraded-environment measurement, not a healthy
production collection baseline. Replay measurements are unaffected because
they made no network requests.

---

## U.S. watcher location eligibility

The watcher applies one conservative location gate in
`watcher/eligibility.py::assess_us_location()`. It reads canonical and raw
location fields, remote-location/status fields, and structured country data
preserved by source adapters. Structured country values take precedence over
misleading text within the same location object; unambiguous ISO country codes
and explicit country/region names are also recognized. Strong location phrases
in posting text can resolve a city-only ATS location, while a city name or U.S.
state abbreviation alone never establishes a country.

Any separate explicit U.S. option keeps a multi-location role eligible, as do
U.S.-remote roles. Explicitly foreign and foreign-remote roles receive the
stable watcher reason `outside_us`; missing and genuinely ambiguous locations
continue to normal role eligibility. The gate changes neither backend fit
scores nor role tracks, actions, ranking values, or degree decisions.

---

## Watcher role eligibility

Role classification prioritizes explicit title and core-duty evidence over
incidental technology words elsewhere in a posting. Applied AI integration,
technical digital-solutions/workflow roles, quantitative analyst/trading work,
technical product/APM programs, and umbrella programs with an explicit
technology, analytics, engineering, data, quantitative, or risk-technology
track can enter the software-adjacent watcher.

Naval/mechanical/industrial product design, electrical hardware, consumer
insights/market research, and generic manufacturing quality remain outside the
watcher even when boilerplate mentions AI, Python, software, modeling,
analytics, or testing. Firmware/embedded-software titles and explicit software
QA automation remain eligible. Generic product and umbrella programs without
central technical evidence remain excluded.

---

## Categorical student eligibility

The watcher first confirms that a posting is an internship, co-op, or student
program and that it is open. Only then does it apply conservative
student-status restrictions, followed by location and target-role eligibility.
This keeps full-time senior and manager roles classified as `not_internship`
instead of assigning a categorical student restriction.

Within that gated evaluation, evidence is checked in this order: explicit
structured eligibility fields, title, minimum/required qualifications, then
clear mandatory description language. Preferred-qualification sections and
phrases such as `preferred`, `encouraged`, or `a plus` never exclude by
themselves. A degree keyword without mandatory enrollment context is
insufficient. Negated requirements such as `Advanced degree not required` and
mixed eligibility such as `Bachelor's or master's` take precedence.

Clear restrictions use stable reasons:

- `phd_only`: current PhD/doctoral enrollment or a PhD/doctoral internship.
- `graduate_only`: graduate, master's, MBA, JD, or other advanced-degree
  candidates when no undergraduate option is present.
- `freshman_only`: freshmen/first-year students only, including an explicitly
  restricted rising-sophomore program for students finishing freshman year.
- `returning_intern_only`: returning/former interns, invitation-only return
  programs, or mandatory prior internship at the same employer.

Mixed eligibility stays allowed: undergraduate-or-graduate,
bachelor's/master's/PhD, freshmen-and-sophomores, and similar alternatives.
Ordinary graduation dates, recent-graduate acceptance, preferred experience,
working with PhD researchers, and returning to school after the internship
also remain eligible. Ambiguity always passes.

A categorical exclusion retains the backend role/track and original posting
text, but follows the existing hard-ineligibility convention: watcher fit is
zero, the watcher action is `skip`, and the job is omitted before email/seen
selection. Normal run reports include a bounded audit entry with company,
title, stable reason, evidence source, evidence, preserved role track, and
whether mandatory, negated, or mixed evidence was detected.

---

## Watcher posting audit

`python -m watcher.audit` explains each watcher stage without loading alumni
data. The safe default is state-only: it reads the latest sanitized comparison
snapshot and notification records without making network requests.

```bash
PYTHONPATH=.:backend python3 -m watcher.audit --company Google
PYTHONPATH=.:backend python3 -m watcher.audit --company Uber --title "Software Engineering Intern"
PYTHONPATH=.:backend python3 -m watcher.audit --url "https://jobs.uber.com/en/jobs/300697/"
PYTHONPATH=.:backend python3 -m watcher.audit --requisition-id 300697 --json audit.json
PYTHONPATH=.:backend python3 -m watcher.audit --job-id "<analyzed-id>" --live
```

Queries may use configured company names or aliases, partial titles, exact or
normalized URLs, native requisition IDs, analyzed job IDs, or canonical
identity keys. `--limit 25` bounds ambiguous results. `--live` performs normal
collection, dedupe, analysis, classification, eligibility, scoring, and
identity decisions, but never sends email, primes postings, writes seen rows,
persists source-health attempts, or requires alumni data.

The trace independently reports collection/provenance, watchlist matching,
identity, deduplication and its exact merge tier, season, internship/open and
U.S. status, role confidence/evidence, watcher eligibility, scoring, historical
notification state, and one stable final reason. `--json` emits stable JSON.

The same command exposes the bounded source comparison:

```bash
PYTHONPATH=.:backend python3 -m watcher.audit --comparison
PYTHONPATH=.:backend python3 -m watcher.audit --comparison --live \
  --comparison-json source-comparison.json \
  --comparison-markdown source-comparison.md
```

Source comparison evaluates one immutable lightweight outcome for every job,
so category counts always cover the complete posting universe. It then applies
the deterministic detail policy before constructing or recursively sanitizing
rich traces. Earlier decisive watchlist/season/internship/open reasons defer
location and notification expansion until a detail is selected, through the
same shared evaluator. Each detailed run keeps all eligible comparisons, no-posting
coverage, non-routine rejections, and operational anomalies; routine rejection
reasons retain a deterministic sample of 25 postings per reason. A hard
ceiling of 2,000 details per run bounds unusual cases too.

Reports use schema version 2 and expose `postings_evaluated` plus
`detail_entries_retained`; the existing `entries` field contains only selected
sanitized rich traces. SQLite retains exact aggregates for 30 runs and those
selected details for the newest three runs. The report builder owns selection;
the store persists the provided ordered entries and applies only a defensive
hard ceiling. Schema-version-1 persisted reports remain readable.

Retention cleanup runs transactionally with the comparison save and never
touches notification or source-health tables. SQLite compaction is not an
hourly default: `VACUUM` runs only after cleanup deletes at least 500 detail
rows and at least 25% of database pages are free. GitHub Actions uploads the
sanitized health and source-comparison JSON reports for 14 days and appends the
bounded Markdown comparison to the job summary.

On the fixed 11,855-job warm replay, the lightweight-first implementation
retained 180 rich details and reduced median source-comparison time from the
approximate 20-second baseline to 4.246 seconds. Rich construction and
sanitization together took 0.124 seconds; complete counts still cover all
11,855 jobs.

## Watcher source health

Every watcher execution assigns a unique run ID and records exactly one direct
source outcome for every configured company plus one outcome for every
configured GitHub listings feed. This is operational health, not opening
availability: a direct source that successfully returns zero jobs is responding
correctly, and a GitHub feed that validates but has zero watchlist-matching rows
is healthy.

Direct sources use these deterministic states:

- `healthy`: the latest fetch succeeded with one or more rows.
- `empty`: the latest fetch succeeded with zero rows and has not met the
  repeated-empty threshold.
- `degraded`: the latest fetch failed once or twice, or a previously productive
  source has returned zero rows in at least two consecutive successful runs.
- `failing`: the latest fetch failed at least three consecutive times.
- `unsupported`: the company is intentionally `bespoke` or `github_only`; no
  direct request was attempted and failure counters do not advance.
- `unknown`: no usable attempt state exists.

GitHub feeds are `healthy` after any valid payload, including zero matching
rows; one or two consecutive failures are `degraded`, and three or more are
`failing`. Status changes are reported once as transitions. A transition from
`degraded`/`failing` to `healthy`, or to `empty` after the endpoint responds
again, is a recovery. Initial states are not treated as transitions or false
recoveries.

Per-company effective coverage is reported separately:

- `direct_covered`: direct succeeded with one or more rows.
- `direct_empty_but_responding`: direct succeeded with zero rows.
- `backstop_only`: an intentionally unsupported direct source has at least one
  successfully responding configured GitHub feed.
- `direct_degraded_backstop_available` or
  `direct_failing_backstop_available`: direct failed this run, but a GitHub feed
  responded; persistent direct health chooses degraded versus failing.
- `uncovered_for_run`: direct failed or is unsupported and every configured
  GitHub feed failed. Merely finding no active posting never makes a company
  uncovered.

Health history lives in the existing watcher `seen.sqlite` file, so the current
`watcher-data` branch persistence automatically carries both seen-job and
source-health history. Opening an older database safely adds
`source_health_attempts` and `source_health_current` with `CREATE TABLE IF NOT
EXISTS`; it does not delete, rename, or rewrite `seen`. Deleting `watcher-data`
resets both histories. The next run initializes successes as `healthy`/`empty`,
first failures as `degraded`, and unsupported sources as `unsupported`, without
emitting recovery alerts.

The final Actions heartbeat forwards the exact last one-line application
heartbeat and appends only `seen_loaded`, `seen_saved`, and `seen_store`, so
current and future application fields are preserved automatically. The
application heartbeat includes comma-safe integer fields:
`companies_configured`, `direct_healthy`,
`direct_empty`, `direct_degraded`, `direct_failing`, `direct_unsupported`,
`github_feeds_healthy`, `backstop_only_companies`, `uncovered_companies`,
`health_transitions`, and `health_recoveries`. GitHub Actions also writes the run
ID, run counts, health aggregates, seen-store status, and actionable details to
the job summary. It emits transition-only warnings for newly degraded/failing
sources and recoveries, plus an error annotation for each currently uncovered
company; these annotations do not fail the watcher run.

Set `WATCHER_HEALTH_REPORT_PATH` or pass `--health-report` to write the sanitized
JSON report used by the workflow. Source-health email has a separate renderer,
send call, cooldown state, and daily-summary state from the internship digest.
It never calls posting email/prime writes and never includes alumni data.

Configure it independently with:

- `WATCHER_HEALTH_EMAIL_MODE`: `off`, `transitions_only` (default),
  `failure_only`, or `daily_summary`.
- `WATCHER_HEALTH_EMAIL_HOUR_UTC`: daily-summary hour, default `12`.
- `WATCHER_HEALTH_ALERT_COOLDOWN_HOURS`: repeated-failure cooldown, default
  `24`.
- `WATCHER_FEED_STALE_HOURS`: configured-season feed inactivity threshold,
  default `48`.

`transitions_only` sends new failures, recoveries, newly silent productive
direct boards, coverage regressions, and both-tier outages. `failure_only` also
allows continued failures after cooldown. `daily_summary` sends at most once
per UTC day after the configured hour and includes source-state totals,
backstop-only/uncovered companies, recent transitions, stale feeds, and source
comparison counts. A structurally valid GitHub fetch with zero matching roles
is not a failure; stale-feed alerts require prior configured-season activity.

Inspect a local database with SQLite:

```sql
select company, adapter, status, consecutive_failures, last_rows_returned
from source_health_current
order by status, company;

select observed_at, company, adapter, succeeded, rows_returned, error_kind
from source_health_attempts
order by attempt_id desc
limit 100;
```

All health tests are offline: source adapters are faked, timestamps/run IDs are
fixed, and SQLite files are temporary.

---

## Watcher Alumni Matching

The scheduled watcher can use alumni matching in GitHub Actions without
uploading the full private alumni spreadsheet. Generate a compact JSON map that
contains only alumni attached to companies in `watcher/watchlist.yml`; keep this
file private and do not commit it.

**Step 1. Generate the compact alumni map**

```bash
python scripts/build_watcher_alumni_map.py --csv "C:\path\to\alumni.csv" --watchlist watcher/watchlist.yml --out private/company_alumni.json
```

The script prints the number of alumni records written, the number of companies
with alumni, the number of watchlist companies checked, and a short list of
companies with matches.

**Step 2. Base64 it in PowerShell**

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("private/company_alumni.json")) | Set-Clipboard
```

**Step 3. Create a GitHub Actions secret**

Create a repository secret named `WATCHER_COMPANY_ALUMNI_JSON_B64` and paste the
base64 value from your clipboard.

**Step 4. Rerun the watcher**

Confirm the workflow log says something like:

```text
ALUMNI: status=loaded-json-map records=12 employers=8
```

The email digest should say `Alumni index: X records across Y employers`, and
jobs at companies in the map should show the matching alumni instead of
`Alumni matching disabled; roster not loaded`.

## What it does

1. **Ingest & clean** — sniffs the delimiter, normalizes messy headers
   (`"Pay"`, `" Job Title "`, `"Remote?"`, `"Apply By"` → canonical columns),
   strips nullish cells (`N/A`, `-`, `none`), fixes unicode dashes/NBSPs, and
   reports every unmapped or colliding column instead of silently dropping it.
2. **Dedupe** — collapses exact and near duplicates (case/whitespace variants,
   URLs that differ only by `utm_*` tracking), merging any fields the kept row
   was missing, with a per-merge report line.
3. **Infer** — fills obviously-derivable blanks (remote status from the text,
   location from remote status, summer/fall term from the description) and
   labels each row with what was inferred.
4. **Parse compensation** — `$25/hr`, `$4k/month`, `25-30/hour`, `80k`,
   `$3,000 for the summer`, `₹1.5L–₹2.4L` all normalize to a USD/hour range
   with a confidence score and explicit notes for every assumption
   (assumed period, assumed currency, the INR lakh/LPA convention).
5. **Classify** — company type (tech / startup / non-tech / unknown) and role
   (SWE / DS / ML-AI / quant / product / IT / non-technical / unknown), each
   with confidence and the evidence used.
6. **Flag** — red flags (unpaid, equity-only, commission-only, pay-to-work
   scams, "no interview" hiring, WhatsApp recruiting, 3+ years required for an
   internship, founder-responsibility dumping, 10+ tool laundry lists,
   grunt work with no learning) and positive signals (stack match against your
   profile, pay level, ownership, mentorship, conversion path, reputable
   employer, concrete tech stack, backend focus, startup environment).
7. **Score** — transparent 0–100 with eight weighted categories, top reasons,
   top concerns, and a recommended action (apply now / apply later /
   research more / skip).
8. **Ask** — a natural-language box answered by a deterministic query
   interpreter (details below).

Scoring benchmarks, including the clean-commit independent U.S. holdout
workflow, are measurement-only and documented in `evaluation/README.md`.
Holdout tooling must be committed before a later clean run collects artifacts;
the exporter never uses email, alumni data, or production seen state.

## Company classification is layered, not name-matching

Per the brief, "tech company" is decided by evidence, not vibes:

1. **Known lists** (`data/known_companies.json`, editable) — highest trust.
2. **Name tokens** — "Technologies", "Labs", "…AI", ".ai", "Robotics" etc.
3. **Posting context** — 3+ technical-stack terms in the description ⇒ tech;
   startup language ("seed-funded", "Series A", "8-person team") ⇒ startup,
   even without a heavy stack; bakery/retail/staffing terms ⇒ non-tech.
4. **Role guard** — a clearly technical role title prevents a non-tech verdict
   from weak name evidence alone; the company stays `unknown — kept for review`.

Every verdict ships with `confidence` and `evidence[]`, shown in the UI.

## The scoring model

`score = Σ (category_score × weight)`, then hard rules. Weights live in
`backend/app/config.py` and sum to 1.00:

| Category | Weight | What it measures |
|---|---|---|
| role_relevance | 0.22 | Role type × your profile's role affinities |
| compensation | 0.16 | USD/hr band; unpaid=0, equity-only≈5 |
| legitimacy | 0.16 | Starts at 70; −30 per critical, −12 per major, −4 per minor flag; +12 reputable |
| learning_value | 0.14 | Mentorship, ownership, structured program, conversion |
| technical_depth | 0.12 | Concrete tools named; capped low for non-technical roles |
| effort_vs_value | 0.08 | Application hoops vs. what you get |
| location_convenience | 0.06 | Remote or near your preferred locations |
| deadline_urgency | 0.06 | Time pressure; expired = 0 |

**Hard rules (applied after the weighted sum):**

- Any **critical** flag (e.g. asks applicants to pay) ⇒ total capped at 40,
  bucket `low`, action `skip` — headline pay cannot rescue a scam.
- **Three or more major flags** ⇒ capped at 44, `low`, `skip` — a pattern, not
  a coincidence.
- **Expired deadline** ⇒ action `skip` regardless of score.
- Score ≥ 70 with no major flags ⇒ `apply_now`; ≥ 60 with a deadline inside
  7 days ⇒ `apply_now`; ≥ 55 ⇒ `apply_later`; ≥ 45 ⇒ `research_more`.

Buckets: **high ≥ 70**, **maybe 45–69**, **low < 45**. Every category returns a
one-line explanation; the drawer renders all of them, so any score can be
audited by clicking.

## "Ask the dataset" — deterministic by design

`backend/app/ask.py` splits the feature into two functions:

- `interpret(question) -> QueryPlan` — keyword/regex rules producing a small,
  inspectable plan: `{intent, role, paid_only, remote_only, keywords}`.
- `run_plan(plan, jobs) -> answer` — pure filtering/ranking over already-scored
  jobs.

Canonical questions it understands (also offered as suggestion chips):
best-for-backend, paid DS only, exploitative, actual startups, apply tonight —
plus paid/unpaid/remote/role modifiers and a keyword fallback. Every answer
echoes its interpretation and applied filters, and carries
`llm_note: "Answered by deterministic rules — no LLM involved."`

**LLM integration point:** replace only `interpret()` (marked
`# === LLM INTEGRATION POINT ===`, with an `ask_with_llm()` stub). An LLM would
translate free text into the same QueryPlan schema; `run_plan` stays
deterministic, so answers remain grounded in the actual rows.

## The sample dataset

`data/sample_postings.csv` — 31 rows, 29 unique. Intentionally messy: dirty
headers, an exact duplicate, a near-duplicate (case/whitespace + `utm_` URL),
blank fields to infer, eight-plus pay formats, INR salaries, an unpaid
"exposure" role, an equity-only founder-dump, a commission-only cold-calling
role, a $99-fee WhatsApp scam, a data-entry role disguised by an "Analytics"
employer name, ambiguous company names (Meridian, Kite, Orchid), and one
expired deadline. Expected result with the bundled profile: 16 high / 5 maybe /
8 low, 2 duplicates merged.

Note: the sample's deadlines were written relative to June 2026; the backend
tests pin `today = 2026-06-09` so they stay deterministic. The live app always
uses the real current date, so deadline-related output will naturally shift.

## Architecture

```
internship-signal/
├── backend/
│   ├── app/
│   │   ├── main.py        FastAPI routes (ingest, jobs, summary, ask, profile)
│   │   ├── ingest.py      pipeline orchestration + cleaning report
│   │   ├── normalize.py   header mapping, cell cleaning, inference, dates
│   │   ├── dedupe.py      canonical keys, URL normalization, merge report
│   │   ├── salary.py      compensation parser → USD/hr + confidence + notes
│   │   ├── classify.py    layered company classifier + role classifier
│   │   ├── signals.py     red flags, positive signals, profile match
│   │   ├── scoring.py     weighted categories + hard rules + actions
│   │   ├── ask.py         interpret() / run_plan() + LLM integration point
│   │   ├── profile.py     student profile (data/profile.json overridable)
│   │   ├── config.py      weights, thresholds, FX table, paths
│   │   └── store.py       in-memory dataset store
│   └── tests/             86 tests across 8 files
├── frontend/
│   └── src/
│       ├── App.jsx        tabs: Overview / Postings / Buckets / Ask
│       ├── components/    table, drawer, dashboard, board, ask, upload…
│       ├── utils/         pure: filtering, sorting, formatting, CSV export
│       ├── hooks/         localStorage shortlist
│       └── __tests__/     20 vitest tests
└── data/                  sample CSV, known-companies list, profile
```

Flow: CSV → normalize → dedupe → per-row (parse comp → classify role →
classify company → flags/signals → score) → summary. The dataset is stored
in memory under a short id; the frontend keeps the full scored array and does
filtering/sorting client-side. Job ids are stable content hashes
(`sha1(company|title|location)[:10]`), so the localStorage shortlist survives
re-ingesting the same file.

## UX touches

- **Cleaning report** — exactly which columns mapped where, what collided,
  which rows merged (and which fields were filled), what was inferred, and
  how many salaries parsed vs. needed assumptions.
- **Signal bar** — the same horizontal score meter everywhere (table, drawer,
  board), with click-to-explain per-category bars and visible weights.
- **Profile-match chips** — "why this matched you": the exact skills/interests
  that overlapped.
- **Confidence dots** on every inferred verdict (role, company type, salary
  parse), with the evidence one click away.
- **Shortlist + export** — star postings (persists across sessions), then
  export exactly the filtered view as a clean CSV.
- **Action board** — postings grouped by apply-now / apply-later / research /
  skip, with days-left or the top concern on each card.
- **Ask interpretation echo** — every answer shows how the question was parsed
  and which filters ran.

## Tradeoffs & limitations (deliberate)

- **In-memory store** — datasets vanish on backend restart. Right for a local
  tool; swapping in SQLite is a ~50-line change confined to `store.py`.
- **Regex classifiers** — fast, explainable, testable; they will misread
  genuinely novel phrasing. Confidence scores and evidence make the misses
  visible instead of silent.
- **Rough FX + conventions** — static currency table; INR lakh amounts without
  a period are read as per-annum (LPA convention) and labeled as such.
- **Client-side filtering** — instant for hundreds of rows; thousands would
  want server-side pagination.
- **No auth / multi-user** — single-user local tool by design.

## What I'd improve next

1. SQLite persistence + dataset history ("compare this week's scrape to last").
2. Optional LLM behind `interpret()` (the seam already exists) with the
   deterministic engine as fallback and for answer verification.
3. Per-field weight editor in the UI writing back to `profile.json`.
4. Browser-extension or paste-a-URL ingestion to skip the CSV step.
5. Embedding-based dedupe for same-role-different-wording postings.
