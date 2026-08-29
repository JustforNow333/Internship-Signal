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
A "row" is just a dict whose keys are `CANONICAL_COLUMNS` (owned by
`internship_signal/domain/jobs.py` and re-exported by `normalize.py`):

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
    ┌─ Tier 1: direct ATS adapter (Greenhouse/Lever/Ashby/SmartRecruiters/iCIMS/SuccessFactors/Workday/bespoke)
    │     └─ success → canonical rows tagged source="direct"
    │     └─ fail/blocked → log, continue (do NOT abort the run)
    │
    └─ Tier 2 (always, as backstops): typed GitHub feeds
          ├─ Simplify structured listings.json
          └─ configured Markdown internship tables
              └─ canonical rows tagged source="github"
        │
        ▼
Merge all sources → analyze_rows() → scored jobs (existing engine)
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

## 2. Code layout

Keep shared primitives neutral and watcher implementation in its focused
owners; compatibility facades retain established import paths.

```
internship-signal/
├── internship_signal/domain/   neutral schema, identity, eligibility primitives
├── backend/app/...             analysis, APIs, persistence, hosted product
└── watcher/
    ├── config/                 models, env, loader, validation; package facade
    ├── sources/                focused shared modules + provider adapters
    │   ├── registry.py         sole direct-ATS registry and construction owner
    │   ├── direct.py           narrow matching record/diagnostic lifecycle
    │   ├── base.py             compatibility facade only
    │   └── __init__.py         lazy package compatibility facade
    ├── health/                 models, state, coverage, store, policy, output
    ├── collection.py           source execution, outcomes, diagnostics
    ├── pipeline.py             run_once orchestration
    ├── reporting.py            reports and heartbeat output
    ├── cli.py                  argument parsing and startup
    ├── run_logging.py          stable watcher logger and timing
    ├── run.py                  compatibility facade + python -m entry point
    ├── source_health.py        source-health compatibility facade
    ├── health_alerts.py        health-alert compatibility facade
    ├── text_safety.py          dependency-free failure-path text conversion
    ├── season.py               configured-term staleness checks
    ├── watchlist.yml           per-company config (see §3)
    └── tests/                  offline fixtures and regression tests
```

Reuse, do not reimplement: centralized posting identity and URL/company
normalization from `dedupe`, plus `ingest.analyze_rows`. The watcher must never
compute its own score or treat the analyzed content-hash ID as an ATS ID.

---

## 3. Watchlist config (`watchlist.yml`)

The per-company labor is this table, not code. One entry per company.

```yaml
defaults:
  terms: ["Summer 2027"]          # required; recruiting cycles are explicit
  github_listing_sources:
    - name: simplify
      format: simplify_json
      url: https://raw.githubusercontent.com/OWNER/REPO/BRANCH/path/listings.json
    - name: community_markdown
      format: github_markdown_table
      url: https://raw.githubusercontent.com/OWNER/REPO/BRANCH/README.md
      default_term: Summer 2027
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

`ats` ∈ {bain, epic, ibm, greenhouse, lever, ashby, smartrecruiters, workable,
workday, icims, successfactors, paylocity, bespoke, github_only}. `config.py` validates every entry at startup and fails loudly on
an unknown `ats` or a `bespoke` entry whose module is missing.

`defaults.terms` must be present and contain at least one nonblank term. A
company inherits those terms unless it declares its own nonempty `terms` list;
an explicitly empty company override is an error. Terms are not inferred from
the calendar because choosing the recruiting cycle is a user decision.
`defaults.github_listing_sources` is a list of named, typed, validated HTTP(S)
feeds. Supported formats are `simplify_json` and `github_markdown_table`; the
latter requires a nonblank `default_term`. Adding another compatible feed is a
configuration-only change. Legacy `defaults.github_listing_urls` remains
supported as Simplify JSON. URLs are credential-free and unique after removing
query/fragment. No recruiting-year URL is embedded in Python.

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

**iCIMS** uses one adapter with an explicit `icims_variant`. `jibe_json`
enumerates `GET /api/jobs?limit=100&page=N`, requires a stable nonnegative
`totalCount`, validates nested `jobs[].data`, and uses the portal-namespaced
`req_id` plus the posting-specific same-host canonical URL. `classic` parses
only `GET /jobs/search?ss=1&in_iframe=1&pr=N`, requires the iCIMS listing and
current-page contracts, derives identity from the numeric `/jobs/{id}/.../job`
path, and rejects the outer iframe shell. An exact Jibe zero requires both an
empty `jobs` list and zero total; a classic zero requires the explicit listing
page no-jobs message.

Configuration requires `icims_variant` and `icims_host`. `icims_portals` is an
optional complete ordered host list for reusable multi-portal sources; its
hosts are enumerated independently and combined with portal-namespaced IDs.
One failed or incomplete portal fails the company attempt, while an explicit
empty portal may coexist with populated siblings. No per-job enrichment is
used.

**SuccessFactors** uses the anonymous Career Site Builder HTML search contract,
`GET /{optional-site-prefix}/search/?q=&locationsearch=&startrow=N`. It derives
page size and completion from explicit result/page metadata, rejects repeated
or inconsistent pages, and uses same-host numeric posting-detail IDs. A
retryable page fetch gets at most three attempts and each crawl gets at most
five retries. If a credible total changes, all rows and pagination state are
discarded and one fresh crawl starts at offset zero; only a fully consistent
replacement crawl succeeds. The adapter never performs per-job enrichment.

**Bain & Company** uses the official anonymous, referer-gated careers-search
API. `start` is a zero-based page number; stable `totalResults`, full bounded
enumeration, repeated-page checks, numeric `JobId`, and posting-specific Bain
detail/program URLs are required.

**IBM** uses the official anonymous `www-api.ibm.com` `careers2` search index
with `appid=careers`, `sortby=url`, `fr`/`nr`, and one-based page metadata. A
result is trusted only after two consecutive complete snapshots agree on the
total, sanitized page membership, canonical rows, duplicates, and parse
diagnostics. At most three complete passes are attempted and passes are never
unioned. Only posting-specific `careers.ibm.com` URLs are accepted.

**Epic** requires exact ID-set agreement between the official server-rendered
Next.js Flight jobs contract and `/cached-api/jobs/search/`. Published listing
metadata and the native numeric Avature ID produce posting-specific
`epic.avature.net/Careers/FolderDetail` rows. The standard Avature SearchJobs
board is not authoritative and is never used as collection input.

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
`WATCHER_SEND_EMAIL=0` and must not use `--prime-seen`.

### Bespoke (`sources/bespoke/*.py`)

Google, Amazon, Bloomberg, etc. run custom sites. One module each, highest
value (alumni cluster there) but custom and fragile. Several expose an internal
JSON search endpoint — prefer that over HTML parsing. If a site uses Cloudflare
or other anti-bot challenges, do **not** escalate to headless-browser evasion;
mark the company `github_only` in a comment and let Tier 2 cover it. Document
the decision in the module.

### Tier-2 backstops

#### Simplify JSON (`sources/github_listings.py`)

One GET per configured `simplify_json` entry. As of July 15,
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

#### Markdown tables (`sources/github_markdown_table.py`)

Find the internship table by normalized
`Company | Role | Location | Apply | Added` headers and a valid Markdown
separator row, never by fixed line number. Parse escaped Markdown safely and
extract the real HTTP(S) application target from Markdown links. Retain valid
rows from mixed malformed tables and emit one bounded aggregate warning without
README content; missing/invalid tables and nonempty all-malformed tables fail.

Record `🔒` as closed/inactive, `🛂` as no sponsorship, and `🇺🇸` as US
citizenship required, then remove those markers from company/title/location.
Assign the configured `default_term` and apply normal company/term matching.
Store `Added` only as `extra.source_added_date`; never copy it to
`date_posted` or use it for employer-posting freshness.

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

1. Collect rows from every source. Fixed precedence is direct ATS, Simplify
   JSON, then Markdown, independent of configured feed order.
2. Run them through `analyze_rows()`. Shared posting identity uses stable
   source-native ATS requisition/posting ID first, a posting-specific normalized
   application URL second, and normalized company/title/location only as the
   fallback. Careers/search/program landing pages and URLs shared by distinct
   stable IDs are not posting-specific. Preserve the highest-priority canonical
   fields, fill missing fields from lower-priority rows when safe, and never let
   lower-priority closed state override an active higher-priority result.
3. Preserve `extra.source` compatibility and record `primary_source`, ordered
   `sources`, and per-source details. Emit one analyzed job and one
   notification.

A job seen via `direct` is a first-wave hit; a job seen only via `github` is
tagged so the email can say "(via GitHub — may be a few days old)".

---

## 6. Seen-store (`seen_store.py`)

SQLite (stdlib `sqlite3`, no new dependency). This is the durable operational
state and the thing that prevents duplicate emails. Rebuildable static-analysis
artifacts live in a separate cache database described in §15.

```
table seen(
  job_id      TEXT PRIMARY KEY,   -- watcher notification identity storage key
  company     TEXT,
  title       TEXT,
  url         TEXT,
  first_source TEXT,              -- direct | github
  first_seen  TEXT,               -- ISO timestamp
  emailed_at  TEXT,               -- ISO timestamp, null until emailed
  primed_at   TEXT                -- ISO timestamp, null unless explicitly suppressed
)
```

Do not assume the analyzed backend `job["id"]` is a reliable ATS requisition
ID. Collection dedupe and notification suppression use the same shared posting
identity described in §5. A row suppresses a future digest only when
`emailed_at` or `primed_at` is populated. Existing rows with only `first_seen`
and blank notification markers remain pending.

Live runs email pending matches and populate `emailed_at` only after a
successful digest send. Ordinary dry runs preview/report matches without
inserting or updating notification rows. Explicit priming runs email nothing
and populate `primed_at`. Existing databases migrate in place with
`CREATE TABLE IF NOT EXISTS` plus guarded `ALTER TABLE`; never delete or rebuild
the table.

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
runs. Manual dispatch has separate `send_email` and `prime_seen` booleans;
email-disabled runs do not imply priming, and live send plus prime is invalid.
Scheduled priming, if ever needed, uses a separate explicit setting. Steps:
checkout, set up Python, `pip install -r requirements.txt`, run
`python -m watcher.run`. SMTP creds and any tokens come from repo secrets.

**Persisting the seen-store across runs** (Actions runners are ephemeral) —
choose one and document it:
- commit `seen.sqlite` back to a `data` branch each run (simplest, self-
  contained), or
- upload/download it as a workflow artifact, or
- point at external storage.
Pick the committed-branch approach unless the user objects; it needs no extra
infrastructure. Commit only durable `seen.sqlite`; keep the rebuildable
`analysis-cache.sqlite` in an Actions cache, never on the data branch.

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
- Reuse centralized posting identity / `norm_company` / `norm_url` /
  `analyze_rows` verbatim. The bot must never invent its own scoring or
  notification identity scheme.
- Every external fetch is defensive: time out, catch, log, continue. One bad
  source never blocks the others or the email.
- Respect robots/rate limits; space out requests. The GitHub file is one GET —
  do not hammer it. Prefer official JSON endpoints over HTML scraping wherever
  both exist.
- No secrets in the repo. No silent failures.

---

## 14. Persistent source health

Canonical modules under `watcher/health/` own source attempts, deterministic
state updates, transitions/recoveries, effective company coverage,
sanitization, SQLite health persistence, JSON output, and GitHub Actions
summary rendering. They perform no network requests; `watcher/source_health.py`
is the compatibility facade. `watcher/collection.py` calls sources, while
`watcher/pipeline.py` creates the run ID and UTC observation timestamp shared by
all attempts in an execution.

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

---

## 15. Persistent static-analysis cache

Watcher runs may cache date-independent backend analysis artifacts in the sole
`analysis_cache` table of a dedicated `analysis-cache.sqlite` database.
`seen.sqlite` contains only durable notification, source-health, health-alert,
and source-comparison state. The two databases are never attached and never
share a transaction. Backend ingestion remains SQLite- and
watcher-independent and exposes pure functions for existing deduplication,
one-row static analysis, current scoring/final job assembly, and existing score
ordering.

The watcher pipeline is:

`collect → deduplicate → fingerprint → batch cache lookup → analyze misses → score every row → assemble current jobs → sort → filter`

Artifacts contain parsed compensation, role/company classifications, red
flags, positive signals, profile matches, technology matches, the categorical
student-eligibility decision and reusable qualification parsing, plus the
seven date-independent score-category results. Deadline urgency is excluded.
Every run rebuilds the final category mapping and weighted total, reapplies
caps and actions, recomputes deadline/expiration values, generates final
reasons and concerns, creates the current job ID, and assembles current row
fields and provenance. Complete jobs and final scores are never cached.

Fingerprints are SHA-256 over deterministic sorted JSON containing static row
inputs (including canonical location/remote fields and only structured
eligibility data selected by the backend policy), the complete loaded profile,
the complete loaded known-company configuration, and
`STATIC_ANALYSIS_CACHE_VERSION`. Volatile fetch, retry, health, and observation
metadata and current source provenance are excluded unless a static analyzer
begins consuming them. Watcher target roles remain part of dynamic filtering
and therefore are not artifact inputs. Increment the version whenever static
analysis behavior, inputs, technology/profile matching, compensation parsing,
student eligibility, static scoring, or artifact schema changes.

`WATCHER_ANALYSIS_CACHE_ENABLED` defaults to true.
`WATCHER_ANALYSIS_CACHE_PATH` defaults to `analysis-cache.sqlite` beside the
configured seen database. Reads are batched, new artifacts use one cache-only
transaction, and one cleanup per run removes entries not accessed for 30 days.
Invalid JSON, schema mismatches, missing files, and SQLite errors warn and fall
back to fresh analysis without changing watcher completion, durable state, or
notification behavior. One `ANALYSIS-CACHE` INFO summary reports only aggregate
counts and timings; it never logs keys, job text, URLs, or configuration
contents.

Legacy embedded cache tables are moved only by
`scripts/migrate_analysis_cache.py`. Its default operation validates both
databases, copies and verifies valid rows transactionally, and leaves the
source unchanged. Explicit `--remove-source-table` first creates a validated
backup, then drops only the cache table/indexes, vacuums, and revalidates the
durable database. GitHub Actions restores/saves only the dedicated cache with a
daily UTC key containing runner OS and `STATIC_ANALYSIS_CACHE_VERSION`; the
`watcher-data` branch continues to receive only cache-free `seen.sqlite`.

---

## 16. Versioned collection snapshot and replay

Live collection produces one immutable `CollectionBatch` containing its schema
version, UTC capture timestamp, collection-configuration fingerprint, ordered
canonical rows, sanitized errors, source attempts, GitHub feed counters, and
Workday tenant outcomes, request/retry counts, and bounded failure diagnostics.
Both live and replayed batches enter the same post-collection pipeline; replay
does not copy or fork analysis, filtering, alumni, notification selection, or
source-comparison logic.

Snapshots are UTF-8 gzip JSON with a `.json.gz` suffix. Writes use a temporary
file in the destination directory followed by atomic replacement. Serialization
uses sorted compact JSON and a filename-free, zero-mtime gzip header so the
same batch produces identical bytes. Loading validates the complete structure,
rejects unknown fields, and rejects malformed, truncated, or unsupported
versions before processing. Increment the schema version whenever the persisted
structure changes.

The collection fingerprint is deterministic SHA-256 over only ordered company
names/aliases, ATS types and identifiers, company/global collection terms, and
the effective ordered typed GitHub sources. Scoring, profile, known-company,
filtering, alumni, email, seen-store, and static-analysis-cache configuration
is intentionally absent. Replay fails on a mismatch unless
`--allow-collection-config-mismatch` is explicitly supplied.

`--capture-collection-snapshot PATH` performs normal live collection, saves the
batch, and continues normally. `--replay-collection-snapshot PATH` skips every
network source and is permanently operationally read-only: no internship
email, priming/seen update, source-health observation, health alert, persisted
health report, or persisted source comparison. In-memory health and comparison
diagnostics remain available, and the static-analysis cache continues to work.
Capture and replay are mutually exclusive.

Replay's effective date defaults to the captured UTC date so an old collection
is not represented as current. `--today YYYY-MM-DD` is the explicit
date-sensitive test override. Capture/replay logs one bounded
`COLLECTION-SNAPSHOT` summary containing mode, sanitized path, row count,
capture time, and replay fingerprint-match status, never descriptions, feed
URLs, or snapshot contents.

---

## 17. Bounded source comparison

Source comparison is observability-only and cannot affect matching, email,
seen state, scoring, eligibility, collection, or deduplication. Its fixed
pipeline is:

`all jobs → lightweight outcomes → complete counts → deterministic detail selection → rich traces for selected entries → persistence/rendering`

`audit_trace.py` owns one immutable evaluated posting outcome and is the sole
implementation of source sightings, identity, watchlist, internship/open,
season, location, role/watcher eligibility, notification state, final reason,
and merge/anomaly diagnostics. Lightweight `PostingComparisonSummary` values
contain only scalar count/selection data plus an original job index. Rich
`PostingAuditTrace` values consume the same outcome; they do not reevaluate
business rules.

When watchlist, season, internship, or open-state precedence already determines
the lightweight final reason, the shared outcome defers location/eligibility
and seen-notification expansion. If that summary is selected, rich-trace
construction completes those deferred fields through the same evaluator before
serialization. Open candidates still evaluate eligibility and any relevant
notification state while producing their lightweight final reason.

The full job universe is indexed once for generic URLs, seen records, similar
requisitions, duplicate sightings, and cross-source merge relationships.
Category counts and aggregates use every lightweight summary. The report
builder then retains all eligible entries, non-routine rejections, operational
anomalies, and no-posting companies; it deterministically samples 25 routine
rejections per reason with the established SHA-256 key and applies the existing
2,000-entry hard cap and stable display ordering.

Only selected summaries build and serialize rich traces, and recursive
sanitization runs only on those payloads. Schema-version-2 reports keep the
existing `entries` field for selected rich details and add
`postings_evaluated` and `detail_entries_retained`. Counts remain full-universe
counts. The store persists report-selected entries without resampling or
reprioritizing; it may apply only a defensive hard maximum. Thirty aggregate
runs and three detail runs remain retained, and schema-version-1 persisted
reports remain readable.

---

## 18. Opt-in bounded collection concurrency

Production default remains serial; concurrent mode is available for controlled
canaries. `WATCHER_COLLECTION_MODE` accepts `serial` or `concurrent` and
defaults to `serial`. Serial mode is permanently available as the rollback and
diagnostic path. Promoting the production default is a separate, small,
reversible change made only after reviewed canary evidence; one benchmark never
justifies promotion.

Limits are validated at configuration load:
`WATCHER_COLLECTION_MAX_WORKERS` (1–16, default 4),
`WATCHER_WORKDAY_MAX_CONCURRENCY` (1–5, default 1), and
`WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY` (1–4, default 2). Neither the
Workday nor the per-origin limit may exceed the global worker pool. Invalid
values fail loudly. The recommended initial canary configuration is 4 workers,
Workday 1, per origin 2; raising any value requires canary evidence that source
reliability, retries, and response behavior remained stable.

A task runs only when every applicable limit allows it: the global worker pool,
its origin limit, its provider limit (the per-origin bound, so a provider spread
across many hosts cannot exceed it), and the Workday limit for Workday tasks.
The tightest applicable bound wins. Origin and provider keys are built from
scheme, host, and port alone and never include credentials, paths, query
parameters, or other sensitive URL components. Companies sharing an ATS host
share one origin limit; each Workday tenant is its own host but shares the
single Workday provider limit. Semaphores are acquired in one fixed order, so
deadlock is impossible, and dispatch round-robins across scopes so bounded
origins cannot starve the pool.

Collection plans tasks in configuration order, executes them under the active
mode, and applies every outcome in that same order. Rows keep direct-before-
GitHub priority, and errors, source attempts, health keys, GitHub counters, and
Workday request/retry counters are byte-identical to serial collection. Worker
programming errors never propagate from `future.result()`: they become failed
task results with the sanitized exception type preserved, the remaining tasks
still finish, and they are counted separately from ordinary network variability.

Concurrency changes only when existing fetch callables run. Production
collection builds one adapter set per worker thread so per-fetch adapter
diagnostics stay isolated, and shares one thread-safe Workday tenant pacer so
pacing is never weakened. Timeouts, retries, backoff, and source-specific safety
rules are unchanged. Proxy rotation, cookie harvesting, browser automation for
challenges, CAPTCHA or anti-bot circumvention, authentication bypasses, header
or identity rotation, retrying blocked requests through alternate
infrastructure, and undocumented endpoints are all prohibited. Challenge,
forbidden, unauthorized, and rate-limited responses remain normal source
failures.

Runs emit one bounded `COLLECTION-CONCURRENCY` record and add `collection_mode`,
`collection_max_workers`, `collection_max_observed_concurrency`,
`collection_max_observed_origin_concurrency`,
`collection_max_observed_workday_concurrency`, and
`collection_unexpected_task_exceptions` to the application heartbeat. Snapshot
replay performs no collection and reports `collection_mode=none`. Concurrency
metrics are in-memory canary evidence and are deliberately absent from the
persisted collection-snapshot schema.

The shared Workday pacer records each actual tenant start with a monotonic clock
after pacing completes and immediately before adapter fetching begins. It never
holds its lock while sleeping; a waking waiter rechecks the latest actual start
before proceeding. Canary-only telemetry reports the configured start interval,
start count, minimum/median/maximum spacing, numeric violation count, and
deterministically ordered sanitized company/task identifiers with offsets from
the first start. It contains no URLs, credentials, request paths, or posting
content and is absent from snapshots, heartbeats, email, health schemas, and all
SQLite state.

Validation is staged. Stage 1 is a deterministic offline benchmark over
controlled fake delayed sources that confirms exact serial/concurrent batch
equivalence, exact downstream fixture equivalence, ordering invariants,
concurrency limits, failure isolation, and clean executor shutdown. Stage 2 is a
limited live canary over a small representative allowlist across adapter types
with at most one or two Workday tenants. Stage 3 is a full live concurrent dry
collection, run only after the limited canary passes. Every canary uses a
temporary seen database and temporary analysis-cache database, disables
internship email, priming, and seen marking, disables health-alert delivery and
durable health persistence, and performs no source-comparison persistence.
Production SQLite state is fingerprinted before and after and reported.

Before any full live canary, record the branch, commit SHA, working-tree status,
watchlist fingerprint, upstream branch, and fresh remote tip. The concurrency
implementation and canary tooling should be present in that local commit and
pushed to the intended branch. A canary from an uncommitted or unpushed
implementation may be retained as operational evidence but is not
repository-complete evidence; report that state exactly and do not commit or
push without separate authorization. Store detailed reports only under
`evaluation/private/`, and keep the production default serial.

A canary source that returns HTTP 401, HTTP 403, HTTP 429, a challenge response,
or repeated transport failures is recorded and dropped from the remainder of the
canary instead of being retried. Full live runs are separated by at least one
normal collection interval, and a fresh full serial collection is never run
immediately before or after a full concurrent canary; the serial baseline comes
from a recent normal production run, existing serial timing logs, or a serial
run performed in a separate normal collection window.

A canary fails when a recently healthy source becomes blocked or challenged,
429 responses increase, 401/403 responses newly appear, Workday retries
materially increase, successful-source count materially decreases, per-source
row coverage materially decreases without an explained posting change, an
adapter reports an unexpected exception, required attempts or diagnostics are
missing, ordering or deduplication precedence differs, an executor fails to shut
down cleanly, production SQLite state changes, or any email, priming, or
seen-marking path is invoked. Changing live posting counts are never called a
concurrency regression without source-level evidence.

Reports separate deterministic equivalence results, limited live-canary results,
full live-canary results, and the historical serial baseline, and record for
every canary the exact configured limits, maximum observed global, per-origin,
and Workday concurrency, source successes/empties/failures, HTTP 401/403/429
counts, challenge counts, Workday request and retry totals, rows per source,
unexpected exception count, and whether any production state changed.

Promotion review requires three successful full concurrent canaries at the same
4-global / 1-Workday / 2-per-origin candidate limits in separate normal
collection windows, complete source and downstream diagnostics, clean executor
shutdown, stable ordering/precedence, comparable source coverage, no new
blocking behavior, and zero production side effects. Satisfying those criteria
still does not authorize changing code, workflow, or repository-variable
defaults; promotion is a separate small reversible change.
