# Internship Watcher — build spec

A scheduled bot that watches a fixed list of companies for new SWE-internship
postings, scores them with the existing Internship Signal engine, and emails
the user new matches — annotated with which fraternity alumni work there.

This document is written for an autonomous coding agent. It assumes the
existing `internship-signal/` repo (FastAPI backend + React frontend) is
present and its tests pass. The watcher is an **additive module**; it must not
modify the scoring/classification logic or break any existing test.

---

## 0. The one idea that makes this small

The existing pipeline in `backend/app/ingest.py::process_csv` does **everything
after rows exist** — dedupe, salary parse, classify, flag, score, summarize.
A "row" is just a dict whose keys are `CANONICAL_COLUMNS` (defined in
`normalize.py`):

```python
CANONICAL_COLUMNS = [
    "company", "title", "location", "compensation", "description",
    "requirements", "source_url", "date_posted", "deadline",
    "remote_status", "internship_type",
]
```

So the watcher's **only genuinely new work** is producing those dicts from
scrapers instead of a CSV. Once a scraper yields a list of canonical-shaped
dicts, the entire analysis engine is reused unchanged.

**Required refactor (small, do this first):** extract the per-row analysis loop
from `process_csv` into a reusable function so both the CSV path and the
watcher share identical scoring. Add to `ingest.py`:

```python
def analyze_rows(rows: list[dict], today=None) -> list[dict]:
    """Dedupe + analyze + score already-built canonical rows. Returns the
    same job dicts process_csv produces (minus the cleaning report)."""
```

Then have `process_csv` call `analyze_rows` internally. Run the existing 86
backend tests after this refactor — they must still pass. This guarantees the
bot scores postings byte-for-byte the same as the UI.

---

## 1. Architecture

```
GitHub Actions cron (hourly)
        │
        ▼
For each company in watchlist.yml:
    ┌─ Tier 1: direct ATS adapter (Greenhouse/Lever/Ashby/SmartRecruiters/Workday/bespoke)
    │     └─ success → canonical rows tagged source="direct"
    │     └─ fail/blocked → log, continue (do NOT abort the run)
    │
    └─ Tier 2 (always, as backstop): GitHub listings.json
          └─ canonical rows tagged source="github"
        │
        ▼
Merge both sources → analyze_rows() → scored jobs (existing engine)
        │
        ▼
Filter: role == "swe" AND looks like an internship AND active/open
        │
        ▼
Source-priority dedup + seen-store check (emit only genuinely new ids)
        │
        ▼
Alumni join ("you know N people here")
        │
        ▼
Email digest of new matches  +  record emitted ids as seen
```

Tier 1 is the first-wave advantage; Tier 2 guarantees coverage when a direct
scrape breaks or a company isn't directly scrapable. Both run every cycle —
GitHub is not a fallback that only fires on Tier-1 failure, it is a parallel
net whose hits are simply lower-priority (see §5).

---

## 2. New code layout

Add a sibling package; do not scatter files into `app/`.

```
internship-signal/
├── backend/app/...              (existing — only the analyze_rows refactor)
└── watcher/
    ├── __init__.py
    ├── run.py                   entry point: python -m watcher.run
    ├── config.py                load + validate watchlist.yml, env settings
    ├── season.py                pure configured-term staleness checks
    ├── watchlist.yml            per-company config (see §3)
    ├── alumni.csv               the fraternity list (see §6)
    ├── sources/
    │   ├── base.py              Source protocol + canonical-row helpers
    │   ├── greenhouse.py        one adapter, all Greenhouse companies
    │   ├── lever.py
    │   ├── ashby.py
    │   ├── smartrecruiters.py
    │   ├── workable.py
    │   ├── workday.py           per-tenant; fussier (see §4)
    │   ├── bespoke/             one file per custom site (google.py, amazon.py…)
    │   └── github_listings.py   Tier-2 backstop
    ├── filters.py               SWE + internship + open detection
    ├── seen_store.py            SQLite "already emailed" memory
    ├── alumni.py                load + match alumni to companies
    ├── notify.py                build + send the email digest
    └── tests/
        ├── fixtures/            saved JSON/HTML samples per source
        ├── test_sources.py      each adapter parses fixtures → canonical rows
        ├── test_filters.py      swe/internship/open classification
        ├── test_seen_store.py   new vs already-seen
        ├── test_alumni.py       company-name matching incl. fuzzy cases
        └── test_run.py          end-to-end with mocked sources + fake SMTP
```

Reuse, do not reimplement: `dedupe.job_id`, `dedupe.norm_company`,
`dedupe.norm_url`, and `ingest.analyze_rows`. The watcher must never compute its
own score.

---

## 3. Watchlist config (`watchlist.yml`)

The per-company labor is this table, not code. One entry per company.

```yaml
defaults:
  terms: ["Summer 2027"]          # required; recruiting cycles are explicit
  github_listing_urls: ["https://raw.githubusercontent.com/OWNER/REPO/BRANCH/path/listings.json"]
  remote_ok: true

companies:
  - name: "Capital One"
    ats: workday
    token: "capitalone"           # Workday tenant slug
    workday_site: "Capital_One"   # tenant's site id (see §4)
    aliases: ["Capital One Financial", "Capitol One"]  # for alumni/dedup matching
    alumni_match: ["capital one", "capitol one"]

  - name: "Anduril Industries"
    ats: lever
    token: "anduril"
    aliases: ["Anduril"]

  - name: "Bloomberg"
    ats: bespoke
    module: "bloomberg"           # watcher/sources/bespoke/bloomberg.py
    aliases: ["Bloomberg LP", "Bloomberg L.P."]

  - name: "Two Sigma"
    ats: greenhouse
    token: "twosigma"

  - name: "Some Startup"
    ats: github_only               # no direct scrape; rely on Tier 2
```

`ats` ∈ {greenhouse, lever, ashby, smartrecruiters, workable, workday, bespoke,
github_only}. `config.py` validates every entry at startup and fails loudly on
an unknown `ats` or a `bespoke` entry whose module is missing.

`defaults.terms` must be present and contain at least one nonblank term. A
company inherits those terms unless it declares its own nonempty `terms` list;
an explicitly empty company override is an error. Terms are not inferred from
the calendar because choosing the recruiting cycle is a user decision.
`defaults.github_listing_urls` is an inline list of validated HTTP/HTTPS URLs.
It may contain more than one structured feed for regional coverage or overlap
periods. No recruiting-year URL is embedded in Python.

**Agent task — ATS auto-detection helper.** Provide
`python -m watcher.detect "Company Name"` that fetches the company's careers
page and guesses the ATS + token by looking for telltale URLs
(`boards.greenhouse.io/<token>`, `jobs.lever.co/<token>`,
`jobs.ashbyhq.com/<token>`, `*.myworkdayjobs.com/<tenant>/<site>`, etc.). This
turns watchlist construction from research into review. It is a convenience,
not part of the scheduled run.

---

## 4. Source adapters

### Protocol (`sources/base.py`)

```python
class Source(Protocol):
    name: str
    def fetch(self, company: CompanyCfg) -> list[dict]:
        """Return canonical-shaped rows (CANONICAL_COLUMNS keys).
        Must raise SourceError on failure — never return [] to hide an error."""
```

A helper `make_row(**fields)` returns a dict pre-filled with empty
`CANONICAL_COLUMNS` so adapters only set what they have. Always set
`source_url` (enables URL-based dedup in the existing engine) and
`date_posted` when the source provides it.

### Tier-1 ATS adapters

Each reusable adapter hits the platform's standard JSON endpoint. Known shapes
(agent: verify current endpoints at build time, they drift):

Posting-level schema failures are isolated: mixed payloads retain valid rows
and log one bounded aggregate warning, while a nonempty payload with zero valid
canonical rows fails the source. Paginated adapters reject repeated pages
rather than looping. Page/feed-level schema validation remains strict.

- **Greenhouse:** `https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true`
- **Lever:** `https://api.lever.co/v0/postings/<token>?mode=json`
- **Ashby:** public posting API per board token
- **SmartRecruiters:** `https://api.smartrecruiters.com/v1/companies/<token>/postings`
- **Workable:** company subdomain jobs endpoint

These are clean and cover a large fraction of mid-size tech + funded startups.

### Workday (`sources/workday.py`)

Workday is per-tenant: each company has its own host
(`<tenant>.<dc>.myworkdayjobs.com`) and a site id, queried via a POST to the
tenant's CXS search endpoint with a JSON body (pagination via `offset`/`limit`).
Expect more per-company config (`token` + `workday_site`) and more breakage.
Many enterprise/finance names on the list live here.

Posting-level schema damage is isolated: non-object records and records with a
blank title or `externalPath` are skipped, raw page length advances pagination,
and one bounded aggregate warning reports company, retained/skipped totals, and
stable reason counts without raw payloads. Page-level shape errors remain fatal.
A nonempty complete fetch with zero valid canonical rows also remains a schema
failure, while a valid zero-posting board succeeds with an empty result.

#### Workday transport reliability

The shared Workday request path captures only safe response metadata: status,
query-free final URL, content type/encoding, bounded body size, generic body
kind, SHA-256 digest, attempt number, and retryability. Body previews are off by
default; any enabled preview is bounded/redacted. Raw HTML, cookies, sensitive
headers, tokens, and challenge values are never logged, persisted, placed in
health JSON, heartbeats, or email. Responses larger than 16 MiB fail safely.
Gzip/deflate, UTF-8 BOM, safe declared charsets, redirects, empty bodies, and
decode failures are classified explicitly. HTML is a fetch failure, never an
empty board.

Workday alone retries transient failures with three total attempts: HTTP 429,
500/502/503/504, timeout, temporary DNS/connection failures, empty responses,
and potentially transient HTML/non-JSON responses. Plain HTTP 400/401/404 and
plain 403 responses are permanent; a 403 is retryable only when its safely
inspected body is unambiguously a temporary HTML challenge. Valid-JSON schema
and deterministic posting failures are not retried. Backoff is injectable and
bounded to approximately 1–2 seconds after attempt one and 3–5 seconds after
attempt two; a numeric `Retry-After` is capped at 10 seconds.

An instance-local pacer delays the start of different Workday tenant fetches,
not pagination pages within one tenant. `WATCHER_WORKDAY_MIN_INTERVAL_SECONDS`
defaults to `0.5`, permits finite values from `0` through `10`, and `0` disables
pacing. Invalid values fail configuration clearly. No module-level timing state
or concurrency is introduced.

The run labels a likely shared Workday incident when at least five tenants fail
and one supported transient transport classification represents at least 60%
of the Workday failures. It reports attempted/succeeded/failed tenants, retry
attempts, the dominant stable error, and the incident flag in logs, the human
report, sanitized health JSON, Actions summary/annotation, and integer-only
heartbeat fields. Per-company attempts and persistent failure counters remain
unchanged; later successes recover naturally. No browser automation, challenge
bypass, copied cookies, proxy rotation, or other anti-bot evasion is allowed.

`scripts/probe_workday_transport.py` safely probes at most five configured
tenants and reports only company, shard, attempts, status, content metadata,
body length/hash prefix, JSON decode status, and jobs-field presence. The manual
Actions `workday_transport_probe` mode runs it without SMTP, alumni data, a seen
database, or `watcher-data` restore/save. Local probes must explicitly set
`WATCHER_SEND_EMAIL=0` and must not use `--mark-seen-without-send`.

### Bespoke (`sources/bespoke/*.py`)

Google, Amazon, Bloomberg, etc. run custom sites. One module each, highest
value (alumni cluster there) but custom and fragile. Several expose an internal
JSON search endpoint — prefer that over HTML parsing. If a site uses Cloudflare
or other anti-bot challenges, do **not** escalate to headless-browser evasion;
mark the company `github_only` in a comment and let Tier 2 cover it. Document
the decision in the module.

### Tier-2 backstop (`sources/github_listings.py`)

One GET per configured `defaults.github_listing_urls` entry. As of July 15,
2026, the active official SimplifyJobs structured feed is
`https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json`.
Despite the historical repository name, live verification found structured
`Summer 2027` rows in that payload. This URL is current configuration, not an
architectural constant; verify it again during each rollover.

Each entry maps to a canonical row:

| listings.json field | canonical field |
|---|---|
| `company_name`      | `company` |
| `title`             | `title` |
| `locations` (join)  | `location` |
| `url`               | `source_url` |
| `date_posted` (unix)| `date_posted` (ISO) |
| `active`            | drop row if `false` |

Filter to entries whose `company_name` matches a watchlist company (via
`norm_company` + aliases) and whose `terms` intersect the configured terms.
Matching is exact after case folding and whitespace normalization. Tag every
row `source="github"`, retain `source_adapter="github_listings"`, and add feed
provenance. Treat the schema defensively: if a file fails to parse or required
keys vanish, log a loud warning identifying that feed and continue with Tier-1
and any other successful feeds. Never turn a failed fetch or invalid payload
into an empty successful result.

### Season status (`season.py`)

Season checking is pure and never makes a network request. It extracts
four-digit years from configured default terms and reports:

- `stale` when every recognized term year is before the current year.
- `rollover_due` in July or later when the newest recognized term year is the
  current year and no future-year term is configured.
- `ok` when any future-year term exists, or before July when a current-year
  term exists.
- `unknown` when no four-digit year can be extracted.

All statuses continue the run. Non-`ok` statuses are prominent warnings, and
stale company-specific overrides are identified by company name. The report,
email header, run result, application heartbeat, and workflow final heartbeat
surface the active terms and status. Heartbeat terms use underscores for spaces
and `|` between terms so comma-delimited parsing remains safe.

---

## 5. Merge, dedup, and source priority

1. Collect rows from all sources into one list. Tag each with its `source`
   (`"direct"` or `"github"`) in the row's `extra` dict so it survives into the
   job object.
2. Run them through `analyze_rows()` — the existing engine already merges
   duplicates by `job_id` (stable `sha1(company|title|location)`) and by
   normalized URL. **Enhancement:** when two rows merge, keep
   `source="direct"` if either was direct (direct wins). The agent should add a
   tiny merge-time rule for the `source` tag, since the stock merge only fills
   empty fields.
3. After scoring, each job has a stable `id` and a winning `source`.

A job seen via `direct` is a first-wave hit; a job seen only via `github` is
tagged so the email can say "(via GitHub — may be a few days old)".

---

## 6. Seen-store (`seen_store.py`)

SQLite (stdlib `sqlite3`, no new dependency). This is the only persistent
state and the thing that prevents duplicate emails.

```
table seen(
  job_id      TEXT PRIMARY KEY,   -- existing dedupe.job_id
  company     TEXT,
  title       TEXT,
  url         TEXT,
  first_source TEXT,              -- direct | github
  first_seen  TEXT,               -- ISO timestamp
  emailed_at  TEXT                -- ISO timestamp, null until emailed
)
```

Flow each run: compute `job_id` for every filtered match → `id NOT IN seen` is
new → email those → insert them with `emailed_at`. Because `job_id` is content-
derived and identical to the UI's, re-runs and re-scrapes never re-notify.

Edge rule: if a job first appeared via `github` and is later seen via `direct`,
it is **not** new (already emailed) — do not re-send. The point of first-wave is
the *first* notification; a later direct sighting of an already-known job adds
nothing.

---

## 7. Filters (`filters.py`)

Apply **after** scoring, reading the existing job fields:

- **SWE only:** `job["role_classification"]["role"] == "swe"`. (The existing
  classifier already handles this; do not re-detect.) Make the target role set
  a config constant so the user can later widen to `data_science`/`ml_ai`.
- **Internship, not full-time:** check `internship_type` is set, or
  title/description match `intern|internship|co-op|summer 20\d\d`. Exclude
  new-grad/full-time titles.
- **Open:** drop rows the source marked inactive; drop expired deadlines
  (`job["deadline_days_left"] < 0`).
- **Optional quality gate:** the user may set a `min_score` (e.g. only email
  `apply_now`/`apply_later`). Default off — for a watched target company the
  user probably wants to know regardless. Make it a config flag.

---

## 8. Alumni join (`alumni.py`)

This is the differentiator — the repo and every other job-alert tool stop at
"a job exists"; this says "and here's who you know."

- Load `alumni.csv` (columns: First Name, Last Name, Occupation, Employer,
  LinkedIn URL — matches the user's existing export).
- Build an index keyed by `norm_company(Employer)` (reuse the existing
  function — it already strips Inc/LLC/Ltd and normalizes case/punct, which
  handles "Capital One" vs "Capitol One"? No — that's a typo, not a suffix).
- **Matching rules, in order:** exact on `norm_company`; then watchlist
  `aliases`/`alumni_match` lists; then a conservative fuzzy pass (token overlap
  or small edit distance) for typos like "Capitol One" / "Chainanalysis" /
  "Northgrop groupman". Fuzzy matches must be logged so the user can correct
  the alias list rather than trust silent guesses.
- For each emailed job, attach the list of matching alumni (name, title,
  LinkedIn). Filter alumni to plausibly useful contacts if desired (e.g.
  prefer engineers/recruiters), but default to showing all at that company.

The provided list already contains many target employers (Bloomberg ×2,
Capital One ×3, Workday ×2, Amazon, Google, Salesforce ×2, Oracle, PayPal ×2,
Anduril, etc.), so most direct-scrape hits will carry a referral contact.

---

## 9. Email (`notify.py`)

- One **digest** per run (not one email per posting) to avoid inbox spam; if
  zero new matches, send nothing.
- Transport: stdlib `smtplib` + Gmail app password (documented in README), or
  an env-configured SMTP server. No third-party email SDK required.
- Each posting block: company, title, location, the **score + recommended
  action + top reason** from the engine, any **red flags** (so a scam at a
  target company is still flagged), the apply URL, the `source` tag, and the
  **alumni you know there**. Keep it skimmable.
- All secrets via env / GitHub Actions secrets — never in the repo. `.env`
  stays git-ignored (the repo's `.gitignore` already excludes it).

---

## 10. Scheduler (GitHub Actions)

`.github/workflows/watcher.yml`: cron (e.g. `0 * * * *` hourly — note GitHub
cron is best-effort and can lag under load), `workflow_dispatch` for manual
runs. Steps: checkout, set up Python, `pip install -r requirements.txt`, run
`python -m watcher.run`. SMTP creds and any tokens come from repo secrets.

**Persisting the seen-store across runs** (Actions runners are ephemeral) —
choose one and document it:
- commit `seen.sqlite` back to a `data` branch each run (simplest, self-
  contained), or
- upload/download it as a workflow artifact, or
- point at external storage.
Pick the committed-branch approach unless the user objects; it needs no extra
infrastructure.

**Failure visibility:** the workflow must surface partial failures. A single
company's adapter raising should be caught, logged, and counted — the run
proceeds and the summary log states "N companies OK, M failed (names)". A
totally silent run that just stops emailing is the failure mode to design
against; consider a heartbeat (e.g. a daily "watcher ran, X new, Y errors"
line) so silence is distinguishable from "nothing new."

---

## 11. Testing (must pass before handing back)

- **Do not hit the network in tests.** Save real responses as fixtures under
  `tests/fixtures/` and parse those. Each adapter has a fixture → canonical-row
  test.
- `test_filters.py`: SWE-vs-not, intern-vs-fulltime, open-vs-expired.
- `test_seen_store.py`: first sighting is new; second is not; github-then-direct
  is not re-emailed.
- `test_alumni.py`: exact, alias, and fuzzy (typo) matches; and a non-match.
- `test_run.py`: end-to-end with all sources mocked and SMTP faked — asserts
  only new SWE-intern matches are emailed and the seen-store is updated.
- `test_season.py`: deterministic status rules with injected dates and stale
  company-override warnings.
- Tests never access live sources. Source parsing uses saved fixtures and
  mocked fetches; live rollover verification is a separate manual operation.
- **Regression:** the existing `backend/tests` (86) still pass after the
  `analyze_rows` refactor. State real run output; never claim a pass without
  running it.

---

## 12. Build order (suggested)

1. Refactor `process_csv` → `analyze_rows`; confirm 86 tests still green.
2. `sources/base.py` + Greenhouse + Lever adapters (highest coverage per unit
   effort) with fixtures.
3. `github_listings.py` backstop.
4. `seen_store.py` + `filters.py` + a minimal `run.py` that prints matches
   (no email yet). Verify end-to-end on a 2–3 company watchlist.
5. `alumni.py` join.
6. `notify.py` email + secrets.
7. Remaining adapters (Ashby, SmartRecruiters, Workable, then Workday, then
   bespoke) as the watchlist demands — each behind its own test.
8. `detect.py` helper; populate the full `watchlist.yml`.
9. GitHub Actions workflow + seen-store persistence.

Ship after step 4 is a working, testable core; everything past it is coverage
breadth, added one tested adapter at a time.

---

## 13. Constraints

- Additive only. No change to scoring, classification, salary, or signal logic
  beyond the mechanical `analyze_rows` extraction.
- Reuse `dedupe.job_id` / `norm_company` / `norm_url` / `analyze_rows`
  verbatim. The bot must never invent its own scoring or id scheme.
- Every external fetch is defensive: time out, catch, log, continue. One bad
  source never blocks the others or the email.
- Respect robots/rate limits; space out requests. The GitHub file is one GET —
  do not hammer it. Prefer official JSON endpoints over HTML scraping wherever
  both exist.
- No secrets in the repo. No silent failures.

---

## 14. Persistent source health

`watcher/source_health.py` owns source attempts, deterministic state updates,
transitions/recoveries, effective company coverage, sanitization, SQLite health
persistence, JSON output, and GitHub Actions summary rendering. It performs no
network requests. `run.py` remains responsible for calling sources and creates
one run ID and UTC observation timestamp shared by all attempts in an
execution.

Stable direct keys combine normalized company, `direct`, and configured ATS,
so changing an adapter starts a separate history. GitHub feed keys use a SHA-256
digest of a query-free, credential-free URL label; raw query strings never
appear in keys, heartbeats, or annotations.

Direct state rules, in order:

1. `unsupported` for `bespoke`/`github_only`; no request and no failure-counter
   increment.
2. `failing` after at least three consecutive failed direct attempts.
3. `degraded` after one or two failed attempts, or after at least two
   consecutive successful zero-row runs when that source has previously
   returned a nonzero result.
4. `empty` for any other successful zero-row direct result, including sources
   that have never returned a row.
5. `healthy` for a successful nonzero result.
6. `unknown` only before usable state exists.

GitHub feeds are `healthy` after a valid payload even with zero watchlist rows,
`degraded` after one or two consecutive failures, and `failing` after three.
Fetch, schema, missing-adapter, generic source, and unexpected failures use
stable typed error kinds. Stored error text and feed labels are bounded and
sanitized.

Status changes after initialization are transitions. Recoveries are
`degraded`/`failing` to `healthy`, or to `empty` when a failed direct endpoint
successfully responds with zero rows. Unchanged failing states do not create
another transition. Attempt history is never reset after recovery.

Effective per-company coverage is distinct from posting availability:

- direct success with rows: `direct_covered`;
- direct success with zero rows: `direct_empty_but_responding`;
- unsupported direct plus any successful configured feed: `backstop_only`;
- failed direct plus any successful feed:
  `direct_degraded_backstop_available` or
  `direct_failing_backstop_available` from persistent status;
- failed/unsupported direct plus no successful configured feed:
  `uncovered_for_run`.

The existing `seen.sqlite` owns two additional tables. `source_health_attempts`
is append-only and has `attempt_id`, `run_id`, `health_key`, `observed_at`,
`source_kind`, `company`, `adapter`, `feed_label`, `unsupported_reason`,
`attempted`, `succeeded`, `rows_returned`, `error_kind`, and `error_message`.
`source_health_current` has one row per key with identity/label columns plus
`status`, `previous_status`, `total_attempts`, `total_successes`,
`consecutive_failures`, `consecutive_zero_successes`, `last_attempt_at`,
`last_success_at`, `last_nonzero_at`, `last_rows_returned`, `last_error_kind`,
and `last_error_message`. Attempt insertion and current-state upsert share one
transaction. Legacy databases upgrade via `CREATE TABLE IF NOT EXISTS`; the
`seen` table and its rows are untouched.

Deleting `watcher-data` intentionally resets seen and health history. The next
run initializes nonzero successes to `healthy`, zero successes to `empty`,
failures to `degraded`, and unsupported entries to `unsupported`; initialization
does not emit transition/recovery alerts.

Reports and logs show aggregates, transitions, degraded/failing detail, and all
uncovered companies without listing every healthy/unsupported company. The
application heartbeat preserves existing fields and adds integer-only
`companies_configured`, `direct_healthy`, `direct_empty`, `direct_degraded`,
`direct_failing`, `direct_unsupported`, `github_feeds_healthy`,
`backstop_only_companies`, `uncovered_companies`, `health_transitions`, and
`health_recoveries`. Actions captures the exact last application heartbeat and
forwards it unchanged before appending only `seen_loaded`, `seen_saved`, and
`seen_store`; no second field list can become stale. A missing application
heartbeat is an explicit workflow error rather than a fabricated success.

`WATCHER_HEALTH_REPORT_PATH` or `--health-report` writes sanitized JSON for the
Actions job summary. Actions warns on newly degraded/failing transitions and
recoveries, emits nonfatal error annotations for uncovered companies, validates
both health tables and current-run attempts, and persists the same SQLite file.
There is no source-health email; zero internship matches still send no email.
All automated health tests use fake sources and temporary SQLite files and must
remain offline. Adapter recoveries use existing persisted health state; never
reset `watcher-data` or manually edit a source row to force a recovery.

Operational queries:

```sql
select company, adapter, status, consecutive_failures, last_rows_returned
from source_health_current
order by status, company;

select observed_at, company, adapter, succeeded, rows_returned, error_kind
from source_health_attempts
order by attempt_id desc
limit 100;
```
