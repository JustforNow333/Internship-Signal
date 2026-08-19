# Watcher behavior and contracts

Authoritative description of what the scheduled watcher does. Deployment,
environment, and recovery procedures live in [`operations.md`](operations.md);
tests live in [`testing.md`](testing.md); the completed implementation log lives
in [`history/watcher-progress-archive.md`](history/watcher-progress-archive.md)
and is historical only.

The watcher is **additive**. It must not change backend scoring,
classification, salary parsing, or signal logic.

---

## 1. Pipeline

```
GitHub Actions cron (hourly)
  |
  For each company in watcher/watchlist.yml:
    Tier 1: direct ATS adapter  -> canonical rows tagged source="direct"
            failure -> log, record health, continue (never abort the run)
    Tier 2 (always, in parallel): typed GitHub feeds
            Simplify structured listings.json
            configured Markdown internship tables  -> source="github"
  |
  merge by fixed source priority -> analyze_rows() -> scored jobs
  |
  filter: target role AND internship/co-op AND open
  |
  seen-store partition (emit only genuinely new postings)
  |
  alumni join -> email digest -> record notification state
  |
  source health, health alerts, bounded source comparison
```

Tier 2 is not a fallback that only fires on Tier-1 failure; it is a parallel net
whose hits are lower priority. `watcher/run.py` fetches direct sources before
GitHub so backend dedupe keeps direct provenance. `bespoke` and `github_only`
companies skip direct fetching.

In `collect_rows`, only `None` means "construct defaults" — an explicitly empty
injected source list is preserved.

---

## 2. Watchlist configuration

`watcher/config.py` owns a small dependency-free watchlist loader plus dotenv
loading; process environment values win. Every entry is validated at startup and
fails loudly.

```yaml
defaults:
  terms: ["Summer 2027"]          # required, nonblank
  github_listing_sources:
    - name: simplify
      format: simplify_json
      url: https://raw.githubusercontent.com/OWNER/REPO/BRANCH/listings.json
    - name: community_markdown
      format: github_markdown_table
      url: https://raw.githubusercontent.com/OWNER/REPO/BRANCH/README.md
      default_term: Summer 2027
  remote_ok: true

companies:
  - name: "Capital One"
    ats: workday
    token: "capitalone"                  # Workday tenant slug
    workday_site: "Capital_One"
    aliases: ["Capital One Financial", "Capitol One"]
    alumni_match: ["capital one", "capitol one"]
  - name: "Some Startup"
    ats: github_only                     # no direct scrape; rely on Tier 2
```

- `defaults.terms` is required and cannot be blank. A company inherits it unless
  it declares its own nonempty `terms`; an explicitly empty override is an
  error. Terms are never inferred from the calendar.
- Supported `ats` values (registered in `watcher/sources/registry.py`; see
  [`watcher-sources.md`](watcher-sources.md)): `bain`, `epic`,
  `ibm`, `greenhouse`, `lever`, `ashby`, `smartrecruiters`, `workable`,
  `workday`, `oracle_hcm`, `talentbrew`, `icims`, `successfactors`,
  `paylocity`, `bespoke`, `github_only`. Workday requires tenant, shard, and
  site.
- GitHub feed URLs must be nonblank HTTP(S), credential-free, and distinct after
  removing query/fragment. Supported formats are `simplify_json` and
  `github_markdown_table` (which requires a nonblank `default_term`). Legacy
  `defaults.github_listing_urls` remains supported and is interpreted as
  `simplify_json`. **No recruiting-year URL is hard-coded in Python.**
- Optional `coverage_status: no_source_found` and `platform_family` record
  reviewed coverage gaps without changing collection.
- `python -m watcher.detect "Company Name"` guesses ATS + token from a careers
  page. It is manual-only, never part of a scheduled run, and must never
  fabricate ATS settings.

---

## 3. Source adapters

Adapters live in `watcher/sources/`, implement the `sources/base.py` `Source`
protocol, and **only fetch canonical rows** — no scoring, no eligibility.

```python
class Source(Protocol):
    name: str
    def fetch(self, company: CompanyCfg) -> list[dict]:
        """Return canonical-shaped rows. Must raise SourceError on failure —
        never return [] to hide an error."""
```

`make_row(**fields)` pre-fills empty `CANONICAL_COLUMNS`. Always set
`source_url`; set `date_posted` when the source provides it. Every adapter sets
`extra.source` and `extra.source_adapter`; GitHub rows also keep a safe
`extra.feed_url`.

**Row provenance keys off `extra.source_adapter`, which `make_row` always sets.**
CSV `extra` is user data (an incoming `source` column collides with the
`source_url` alias) and never drives dedupe ordering or provenance.

### Malformed-payload policy (all direct adapters)

- Mixed malformed payloads retain valid rows and emit one bounded aggregate
  warning with stable reason counts and no raw payload content.
- A nonempty payload that yields zero valid canonical rows fails the source.
- Page/feed-level schema validation stays strict; paginated adapters reject
  repeated pages instead of looping.
- A genuinely empty board is a successful empty source.

### Per-platform contracts

Each platform's endpoint, pagination, identity, failure contract, shared
record-parsing rules, and the readiness bar for a new ATS live in
[`watcher-sources.md`](watcher-sources.md). Workday is documented below instead,
because its transport, retry, and pacing rules reach beyond one adapter.

---

## 4. Workday

Workday is per-tenant: `<tenant>.<dc>.myworkdayjobs.com` plus a site id, queried
by POST to the tenant CXS search endpoint with `offset`/`limit` pagination.

**Posting-level schema damage is isolated:** non-object records and records with
a blank title or `externalPath` are skipped, raw page length advances
pagination, and one bounded aggregate warning reports company, retained/skipped
totals, and stable reason counts without raw payloads. Page-level shape errors
remain fatal, a nonempty complete fetch yielding zero valid canonical rows is a
schema failure, and a valid zero-posting board succeeds empty.

**Transport diagnostics** capture only safe metadata: status, query-free final
URL, content type/encoding, bounded body size, generic body kind, SHA-256
digest, attempt number, and retryability. Body previews are off by default and
any enabled preview is bounded and redacted. Raw HTML, cookies, sensitive
headers, tokens, and challenge values are never logged, persisted, placed in
health JSON, heartbeats, or email. Responses over 16 MiB fail safely.
Gzip/deflate, UTF-8 BOM, safe declared charsets, redirects, empty bodies, and
decode failures are classified explicitly. **HTML is a fetch failure, never an
empty board.**

**Retries** apply to transient failures only, three total attempts: HTTP 429,
500/502/503/504, timeouts, temporary DNS/connection failures, empty responses,
and potentially transient HTML/non-JSON responses. Plain 400/401/404 and plain
403 are permanent; a 403 is retryable only when its safely inspected body is
unambiguously a temporary HTML challenge. Valid-JSON schema and deterministic
posting failures are never retried. Backoff is injectable and bounded to roughly
1–2 s after attempt one and 3–5 s after attempt two; a numeric `Retry-After` is
capped at 10 s.

**Pacing** — an instance-local pacer delays the start of *different* tenant
fetches, not pagination within one tenant.
`WATCHER_WORKDAY_MIN_INTERVAL_SECONDS` defaults to `0.5`, accepts `0`–`10`, and
`0` disables pacing. Invalid values fail configuration clearly.

**Identity** — `bulletFields` is tenant-configured display metadata, not a
requisition ID. `_source_id` trusts the first entry only when it is
requisition-shaped (a single token carrying a digit) and does not repeat
`locationsText`; otherwise it returns `""` and identity falls through to the URL
tier. The detail `requisition_id_conflict` guard is unchanged.

**Shared incident** — when at least five tenants fail and one supported
transient transport classification accounts for at least 60% of those failures,
the run reports a likely shared incident with aggregate logs, report detail, an
Actions warning, and integer heartbeat fields (`workday_attempted`,
`workday_succeeded`, `workday_failed`, `workday_retry_attempts`,
`workday_shared_incident`). Every company still records its own failed health
attempt; no counter is reset and no source is declared healthy. A later success
produces the normal one-time recovery transition.

**Prohibited everywhere:** cookies harvesting, proxy rotation, CAPTCHA bypass,
browser automation, header/identity rotation, alternate infrastructure for
blocked requests, undocumented endpoints, or any other anti-bot evasion. Never
reset `watcher-data`, seen rows, or health history.

---

## 5. GitHub backstops (Tier 2)

### Simplify JSON (`sources/github_listings.py`)

One GET per configured `simplify_json` entry.

| listings.json field | canonical field |
|---|---|
| `company_name` | `company` |
| `title` | `title` |
| `locations` (joined) | `location` |
| `url` | `source_url` |
| `date_posted` (unix) | `date_posted` (ISO) |
| `active` | drop row if `false` |

Filter to entries whose `company_name` matches a watchlist company (via
`norm_company` + aliases) and whose `terms` intersect the configured terms;
matching is exact after case folding and whitespace normalization. Tag rows
`source="github"`, retain `source_adapter="github_listings"`, and add feed
provenance. A failed fetch or invalid payload is never turned into an empty
successful result; valid entries survive a mixed malformed payload with one
bounded warning, while an all-malformed nonempty feed fails.

### Markdown tables (`sources/github_markdown_table.py`)

Find the table by normalized `Company | Role | Location | Apply | Added` headers
and a valid separator row — never by line number. Parse escaped Markdown safely
and extract the real HTTP(S) target from Markdown links. Retain valid rows from
mixed malformed tables with one bounded warning; missing/invalid tables and
nonempty all-malformed tables fail.

Record `🔒` (closed), `🛂` (no sponsorship), and `🇺🇸` (US citizenship required),
then strip those markers from company/title/location. Assign the configured
`default_term` and apply normal company/term matching. **`Added` is stored only
as `extra.source_added_date`** — never copied to `date_posted` and never used
for employer-posting freshness. Closed Markdown-only rows are scored but
excluded from notifications.

### Independence

Every configured feed is fetched and health-tracked independently. A valid
payload or table is healthy even when watchlist filtering yields zero matches.
Transport, schema, missing-table, and all-malformed-table failures affect only
that source; other feeds, direct results, digests, and seen handling continue.

---

## 6. Season status

`watcher/season.py` is pure and makes no network requests. It extracts
four-digit years from the configured default terms:

- `ok` — a future-year term exists, or it is before July and a current-year term
  exists.
- `rollover_due` — July or later and the newest recognized year is the current
  year.
- `stale` — every recognized configured year is in the past.
- `unknown` — no four-digit year can be extracted.

Every status continues the run. Non-`ok` statuses are prominent warnings and
stale company-specific overrides are named. The report, digest header, run
result, and heartbeats surface the active terms and status; heartbeat terms use
underscores for spaces and `|` between terms so comma-delimited parsing stays
safe. The rollover procedure is in [`operations.md`](operations.md).

---

## 7. Merge, dedupe, and posting identity

Fixed merge precedence, independent of configuration order:

1. direct company ATS,
2. Simplify structured JSON,
3. configured GitHub Markdown tables.

**Collection dedupe and notification suppression share one posting-identity
policy**, in order:

1. a stable source-native ATS requisition/posting ID,
2. a posting-specific normalized application URL,
3. exact normalized company/title/location as the fallback.

Tracking parameters and harmless URL formatting differences are removed. Careers
landing pages, search results, generic internship/program pages, and URLs shared
by multiple distinct source-native IDs are **not** posting-specific. Different
stable requisition IDs always stay separate even when titles, locations, role
tracks, or generic careers URLs match. The analyzed backend `job["id"]` remains
the existing content hash and is never treated as an ATS requisition ID.

For a genuine duplicate the winner keeps its canonical fields, missing fields
may be filled from lower-priority rows, and provenance records `primary_source`,
every discovering source in `sources`, plus per-source details. **A
lower-priority closed marker never closes an active direct or Simplify result.**
A direct and GitHub sighting of the same requisition produce one analyzed job
and one notification, with direct provenance winning; the digest can still note
"(via GitHub — may be a few days old)" for GitHub-only hits.

---

## 8. Eligibility and filtering

Order of evaluation matters: internship/student-program and open checks run
**first**, then categorical student status, then location, then target role.
This keeps full-time senior and manager roles classified `not_internship`
instead of receiving a categorical student restriction.

`watcher/eligibility.py` is the only watcher-side target-role/degree wrapper.
`watcher/filters.py` then requires eligible, positive-fit, open internships or
co-ops and applies the optional `min_score` gate (default off).

### Internship and open detection (`filters.py`)

- **Target role:** `job["role_classification"]["role"]` must be in the
  configured target-role set (a config constant, so it can widen to
  `data_science`/`ml_ai`). Do not re-detect — the backend classifier owns this.
- **Internship, not full-time:** `internship_type` is set, or title/description
  match `intern|internship|co-op|summer 20\d\d`. New-grad/full-time titles are
  excluded.
- **Open:** rows the source marked inactive are dropped, as are expired
  deadlines (`job["deadline_days_left"] < 0`).

### Categorical student eligibility

Only clear **mandatory** evidence excludes, using stable reasons:

- `phd_only` — current PhD/doctoral enrollment, or a PhD/doctoral internship.
- `graduate_only` — graduate, master's, MBA, JD, or other advanced-degree
  candidates when no undergraduate option is present.
- `freshman_only` — freshmen/first-year only, including a rising-sophomore
  program explicitly restricted to students finishing freshman year.
- `returning_intern_only` — returning/former interns, invitation-only return
  programs, or mandatory prior internship at the same employer.

Evidence is checked in order: explicit structured eligibility fields, title,
minimum/required qualifications, then clear mandatory description language.
Preferred-qualification sections and phrases like `preferred`, `encouraged`, or
`a plus` never exclude by themselves. A degree keyword without mandatory
enrollment context is insufficient. Negated requirements
(`Advanced degree not required`) and mixed eligibility (`Bachelor's or
master's`) take precedence.

**Mixed undergraduate/graduate evidence stays eligible in every source,
including titles:** `_graduate_only` applies `_mixed_degree_eligibility` before
its title short-circuit, exactly as `_phd_only` does. Pure graduate titles such
as `Graduate Student Intern` remain excluded. Ordinary graduation dates,
recent-graduate acceptance, preferred experience, working with PhD researchers,
and returning to school after the internship all remain eligible. **Ambiguity
always passes.**

`Master data`, `master record`, `master dataset`, and `master schedule` are
operational terms, not graduate-degree evidence; bare `master` needs explicit
degree, program, student, or candidate context.

Traces retain mandatory, negation, and mixed-evidence diagnostics. Run reports
include a bounded audit entry with company, title, stable reason, evidence
source, evidence, preserved role track, and which evidence classes were
detected.

### U.S. location gate

`watcher/eligibility.py::assess_us_location()` is the **sole** location gate. It
reads canonical and raw location fields, remote-location/status fields, and
structured country data preserved by adapters.

- Explicit U.S. evidence wins, including across multiple locations, and
  U.S.-remote roles stay eligible.
- Explicit foreign country/region evidence yields the stable reason
  `outside_us`, including foreign-remote roles.
- Missing or genuinely ambiguous locations pass through to normal role
  eligibility.
- Prefer already-collected structured country metadata over free-form text.
  Unambiguous ISO country codes and explicit country/region names are
  recognized. Strong location phrases in posting text can resolve a city-only
  ATS location, but a city name or U.S. state abbreviation alone **never**
  establishes a country.
- Diagnostics are preserved. The gate must never change backend fit scores,
  role tracks, actions, ranking values, or degree decisions.

### Role eligibility

Role classification prioritizes explicit title and core-duty evidence over
incidental technology words elsewhere in a posting.

Eligible with explicit central technical evidence: applied AI integration,
technical digital-solutions/workflow roles, quantitative analyst/trading work,
technical product/APM programs, and umbrella programs with an explicit
technology, analytics, engineering, data, quantitative, or risk-technology
track. Firmware/embedded-software titles and explicit software QA automation
remain eligible.

Excluded even when boilerplate mentions AI, Python, software, modeling,
analytics, or testing: naval/mechanical/industrial product design, electrical
hardware, consumer insights/market research, and generic manufacturing quality.
Generic product and umbrella programs without central technical evidence remain
excluded. IT support, quality/test, and solutions engineering are deliberate
low-priority exceptions capped around 20 unless explicitly changed.

`watcher/season.py` warns on non-`ok` season status but never blocks direct
collection.

### Ineligibility convention

A categorical exclusion retains the backend role/track and original posting text
but sets `watcher_eligible=false` and `fit_score=0`, uses action `skip`, and
omits the job before email/seen selection.

### Scoped company recall overrides

JPMorgan GitHub recall uses only its explicit watchlist aliases. Capital One's
three technology-internship titles use an exact normalized company/title
override, and its retained Workday title may additionally match only the
complete normalized form `technology internship program summer YYYY` (four-digit
suffix years, no extra words). Do not broaden global company matching or generic
technology-intern classification.

---

## 9. Digest and notification state

**Digest policy:** one digest per run, never one email per posting; no default
score gate; ineligible jobs excluded; sorted by fit, generic score, role
priority, company, then title; **zero new matches sends nothing**. Each posting
block carries company, title, location, score, recommended action, top reason,
red flags, apply URL, source tag, and alumni annotations.

**Three explicit modes:**

| Mode | Settings | Effect |
|---|---|---|
| Live send | `WATCHER_SEND_EMAIL=1` (or dispatch `send_email=true`) | Emails pending matches, then writes `emailed_at`. SMTP failure leaves every posting pending. |
| Dry run | `WATCHER_SEND_EMAIL=0`, no `--prime-seen` | Previews/reports matches; inserts or updates **nothing** in the notification table. |
| Explicit prime | email disabled plus `--prime-seen` (or `prime_seen=true`) | Suppresses current pending matches with `primed_at`; email transport is never invoked. |

`send_email=true` with `prime_seen=true` is rejected as incompatible.

**Seen store (`watcher/seen_store.py`)** is stdlib SQLite and the durable
operational state:

```
table seen(
  job_id       TEXT PRIMARY KEY,  -- watcher notification identity storage key
  company      TEXT,
  title        TEXT,
  url          TEXT,
  first_source TEXT,              -- direct | github
  first_seen   TEXT,              -- ISO timestamp
  emailed_at   TEXT,              -- ISO, null until emailed
  primed_at    TEXT               -- ISO, null unless explicitly suppressed
)
```

A row suppresses a future digest only when `emailed_at` or `primed_at` is
populated; legacy rows with neither marker remain pending, so still-open
eligible jobs can enter the next successful live digest. Batch marking is
transactional. Older databases migrate in place with `CREATE TABLE IF NOT
EXISTS` plus guarded `ALTER TABLE` adding nullable `primed_at`,
`analyzed_job_id`, `identity_key`, `requisition_key`, and `location`; the table
is never deleted or rebuilt.

Edge rule: a job first seen via `github` and later via `direct` is **not** new.

---

## 10. Alumni matching

Alumni data is **additive and private**: it never gates, reorders, or rescores
jobs, and it never appears in public artifacts.

Loading priority is compact JSON env/text/path, then CSV env/path. Live sends
require usable alumni data; dry runs may report matching disabled. Alumni CSV
input is explicit UTF-8 with optional BOM, and runtime loading shares one
row-normalization helper with the compact-map builder.

Matching order: normalized exact on `norm_company`, then watchlist
`aliases`/`alumni_match`, then a conservative fuzzy fallback (token overlap or
small edit distance) for roster typos. Fuzzy matches are logged so the alias
list can be corrected instead of trusting silent guesses. Each emailed job
carries its matching alumni (name, title, LinkedIn). Never log private contacts
or SMTP recipients.

Generating the private compact map is documented in
[`operations.md`](operations.md).

---

## 11. Bounded collection concurrency

Concurrency changes only **when** existing fetch callables run. It reorders
nothing and never weakens pacing, timeouts, retries, or backoff.

Collection concurrency is **opt-in in the application**, whose default is
`serial`; serial is the permanent rollback and diagnostic path. **Scheduled
production explicitly selects `concurrent` at `4/1/2`** in
`.github/workflows/watcher.yml`.

| Setting | Default | Range |
| --- | --- | --- |
| `WATCHER_COLLECTION_MODE` | `serial` | `serial`, `concurrent` |
| `WATCHER_COLLECTION_MAX_WORKERS` | `4` | 1–16 |
| `WATCHER_WORKDAY_MAX_CONCURRENCY` | `1` | 1–5 |
| `WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY` | `2` | 1–4 |

Neither scoped limit may exceed the global worker pool, and invalid values fail
configuration loudly. A task starts only when *every* applicable limit allows
it — global pool, origin, provider, and the Workday limit for Workday tasks;
the tightest bound wins. Origin and provider keys use **scheme, host, and port
only**, so no credential, path, or query value can enter a key: companies
sharing an ATS host share one origin limit, and each Workday tenant is its own
host but shares the single Workday provider limit. Semaphores are acquired in
one fixed order so deadlock is impossible, and dispatch round-robins across
scopes so bounded origins cannot starve the pool.

Collection plans tasks in configuration order, executes them under the active
mode, and reduces outcomes in that same plan order. Rows keep direct-before-
GitHub priority, and errors, source attempts, health keys, GitHub counters, and
Workday request/retry counters stay byte-identical to serial collection.
Production collection builds one adapter set per worker thread so per-fetch
adapter diagnostics stay isolated, and shares only one thread-safe Workday
tenant pacer. Worker programming errors never propagate from `future.result()`:
they become failed task results with the sanitized exception type preserved,
remaining tasks still finish, and they are counted separately from ordinary
network variability. Executors must shut down cleanly, and replay creates no
executor or network work.

Prohibited: proxy rotation, cookie harvesting, browser automation for
challenges, CAPTCHA or anti-bot circumvention, authentication bypass, header or
identity rotation, retrying blocked requests through alternate infrastructure,
and undocumented endpoints. Challenge, forbidden, unauthorized, and rate-limited
responses remain ordinary source failures.

Runs log one bounded `COLLECTION-CONCURRENCY` record and add `collection_mode`,
`collection_max_workers`, `collection_max_observed_concurrency`,
`collection_max_observed_origin_concurrency`,
`collection_max_observed_workday_concurrency`, and
`collection_unexpected_task_exceptions` to the heartbeat. Snapshot replay
reports `collection_mode=none`.

The shared Workday pacer records each actual tenant start with a **monotonic
clock after pacing completes and immediately before adapter fetch**, and never
holds its lock while sleeping; a waking waiter rechecks the latest actual start.
Interval, count, spacing statistics, numeric violation counts, and sanitized
relative offsets are **private canary evidence only** — never in snapshots,
SQLite, health data, heartbeats, or email.

Canary and promotion procedure is in [`operations.md`](operations.md).

---

## 12. Persistent static-analysis cache

The watcher caches only **date-independent** backend analysis artifacts in the
sole `analysis_cache` table of a dedicated, rebuildable `analysis-cache.sqlite`.
Durable `seen.sqlite` holds only notification, source-health, health-alert, and
source-comparison state. The two databases are never attached and never share a
transaction. Backend ingestion stays pure, SQLite-independent, and
cache-independent.

Pipeline:

```
collect -> deduplicate -> fingerprint -> batch cache lookup -> analyze misses ->
score every row -> assemble current jobs -> sort -> filter
```

**Cached (static):** parsed compensation, role/company classifications, red
flags, positive signals, profile matches, technology matches, the categorical
student-eligibility decision with reusable qualification parsing, and the seven
date-independent score-category results.

**Always recomputed (dynamic):** deadline urgency and expiration, the final
category mapping and weighted total, caps and actions, final reasons and
concerns, the current job ID, current row fields and `extra` provenance, and
sorting. Complete jobs and final scores are never cached.

Fingerprints are SHA-256 over deterministic sorted JSON of the static row inputs
(including canonical location/remote fields and only the structured eligibility
data the backend policy selects), the complete loaded profile, the complete
loaded known-company configuration, and
`watcher.analysis_cache.STATIC_ANALYSIS_CACHE_VERSION` (currently **8**).
Volatile fetch, retry, health, and observation metadata, current source
provenance, and dynamic watcher target roles are excluded.

> **Bump `STATIC_ANALYSIS_CACHE_VERSION` for any static-eligibility,
> classification, compensation-parsing, signal-detection, profile-matching,
> technology-detection, static-scoring, fingerprint-input, or artifact-schema
> change**, so cached artifacts cannot serve stale decisions. A guard test pins
> the current value.

`WATCHER_ANALYSIS_CACHE_ENABLED` defaults to true and accepts `true`/`false`,
`yes`/`no`, `on`/`off`, `1`/`0`. `WATCHER_ANALYSIS_CACHE_PATH` defaults to
`analysis-cache.sqlite` beside the configured seen database. Reads are batched,
new artifacts are written in one cache-only transaction, and one bounded cleanup
per run removes entries not accessed for 30 days. **Corruption, schema mismatch,
missing files, and SQLite errors are nonfatal**: they warn and fall back to
fresh analysis without opening a transaction against durable state, and jobs and
dedupe reports stay byte-identical.

Each run emits one safe `ANALYSIS-CACHE` INFO summary with aggregate counts and
timings only — never keys, job text, URLs, or configuration:

```text
ANALYSIS-CACHE enabled=true rows=11897 hits=11000 misses=897 invalid=0 writes=897 hit_rate=0.924 lookup_seconds=0.100 static_analysis_seconds=18.000 scoring_seconds=4.000
```

---

## 13. Collection snapshot and replay

Live collection produces one immutable `CollectionBatch` holding its schema
version, UTC capture timestamp, collection-configuration fingerprint, ordered
canonical rows, sanitized errors, source attempts, GitHub feed counters, and
Workday tenant outcomes with request/retry counts and bounded failure
diagnostics. Live and replayed batches enter the **same** post-collection
pipeline; replay never forks analysis, filtering, alumni, notification
selection, or source-comparison logic.

Snapshots are UTF-8 gzip JSON with a `.json.gz` suffix, written through a
temporary file in the destination directory followed by atomic replacement.
Sorted compact JSON plus a filename-free, zero-mtime gzip header make identical
batches byte-deterministic. Loading validates the complete structure, rejects
unknown fields, and rejects malformed, truncated, or unsupported versions before
processing. Schema v2 adds aggregate Workday request/retry and tenant outcome
diagnostics. Increment the schema version whenever the persisted structure
changes.

The fingerprint covers **only collection-affecting configuration**: ordered
company names/aliases, ATS types and identifiers, company/global collection
terms, and the effective ordered typed GitHub sources. Scoring, profile,
known-company, filtering, alumni, email, seen-store, and analysis-cache settings
are deliberately excluded, so those changes stay replay-compatible. A mismatch
fails before analysis unless `--allow-collection-config-mismatch` is explicitly
passed.

`--capture-collection-snapshot PATH` performs normal live collection, saves the
batch, and continues normally. `--replay-collection-snapshot PATH` skips every
network source and is permanently operationally read-only: **no internship
email, no health email, no priming or seen marking, no source-health
observation, no persisted health report, no persisted source comparison.**
In-memory health and comparison diagnostics remain available, and the
static-analysis cache continues to work — that maintenance is replay's only
permitted write. Capture and replay are mutually exclusive.

Replay's effective date defaults to the captured UTC date; `--today YYYY-MM-DD`
is the explicit deterministic test override. Each operation emits one bounded
`COLLECTION-SNAPSHOT` summary with mode, sanitized path, row count, capture
time, and fingerprint-match status — never descriptions, feed URLs, or snapshot
contents. Snapshots contain complete posting text, so `*.json.gz` and the
default `watcher/collection-snapshots/` directory are Git-ignored and must not
be published as CI artifacts.

---

## 14. Source health

`watcher/source_health.py` is pure and makes **no network requests**. It owns
source attempts, deterministic state updates, transitions/recoveries, effective
company coverage, sanitization, SQLite persistence, JSON output, and Actions
summary rendering. `run.py` calls sources and creates one run ID and UTC
observation timestamp shared by every attempt in an execution.

Exactly one direct outcome per configured company and one outcome per configured
GitHub feed is recorded per run. This is **operational health, not opening
availability**.

Stable direct keys combine normalized company, `direct`, and the configured ATS,
so changing an adapter starts a separate history. GitHub feed keys use a SHA-256
digest of a query-free, credential-free URL label; raw query strings never
appear in keys, heartbeats, or annotations.

**Direct states**

| State | Meaning |
|---|---|
| `healthy_with_listings` | valid, complete response retaining ≥ 1 row |
| `healthy_empty` | valid, complete response retaining 0 rows |
| `degraded` | usable rows survived, but skipped malformed/schema-invalid records, material enrichment loss, a failed subrequest, unexpected pagination, or a configured limit makes completeness uncertain |
| `failed` | no trustworthy result survived a fatal transport, access, schema, or collection error |
| `not_configured` | intentional `bespoke`/`github_only`; no request attempted, no failure counter advanced |
| `unknown` | available diagnostics cannot establish a result |

Listing count is independent of health: safe duplicate removal is counted
without degrading a source, and optional enrichment failure stays healthy when
the authoritative listing is complete enough for normal filtering. Every direct
attempt persists bounded retained/malformed/schema-error/duplicate/failed-request
counts, incomplete/truncated/degraded/complete flags, and at most twelve short
reason codes. Payloads, descriptions, secrets, and arbitrary exception text are
never part of this contract.

**GitHub feed states** — `healthy` after any valid payload including zero
matching rows; `degraded` after one or two consecutive failures; `failing` after
three or more. Track every configured source independently.

Status changes after initialization are transitions; recoveries are
`degraded`/`failed` to either healthy direct state. Unchanged failed states do
not create another transition, initialization is never a transition, and attempt
history is never reset after recovery.

**Effective per-company coverage** (distinct from posting availability):
`direct_covered`, `direct_empty_but_responding`, `direct_degraded` /
`direct_degraded_backstop_available`, `backstop_only`,
`direct_failing_backstop_available`, `direct_unknown_backstop_available`, and
`uncovered_for_run` (direct failed or unsupported **and** every configured
GitHub feed failed). Merely finding no active posting never makes a company
uncovered.

**Persistence** — `seen.sqlite` owns two additional tables.
`source_health_attempts` is append-only with `attempt_id`, `run_id`,
`health_key`, `observed_at`, `source_kind`, `company`, `adapter`, `feed_label`,
`unsupported_reason`, `attempted`, `succeeded`, `rows_returned`, `error_kind`,
and `error_message`. `source_health_current` holds one row per key with
identity/label columns plus `status`, `previous_status`, `total_attempts`,
`total_successes`, `consecutive_failures`, `consecutive_zero_successes`,
`last_attempt_at`, `last_success_at`, `last_nonzero_at`, `last_rows_returned`,
`last_error_kind`, and `last_error_message`. Attempt insert and current-state
upsert share one transaction, writes must not alter `seen`, legacy databases
upgrade via `CREATE TABLE IF NOT EXISTS`, and health is nonfatal — it never
affects email or seen marking.

**Heartbeat.** The application heartbeat preserves every existing field and adds
comma-safe integer fields: `companies_configured`, `direct_healthy`,
`direct_empty`, `direct_degraded`, `direct_failing`, `direct_unsupported`,
`direct_healthy_with_listings`, `direct_healthy_empty`, `direct_failed`,
`direct_not_configured`, `direct_unknown`, `github_feeds_healthy`,
`backstop_only_companies`, `uncovered_companies`, `health_transitions`, and
`health_recoveries`. Reports and logs show aggregates, transitions,
degraded/failing detail, and every uncovered company without listing each
healthy or unsupported company.

**Sanitization is total.** Sanitize stored and logged errors, feed labels, keys,
reports, annotations, and heartbeats; never include credentials or raw query
strings. `sanitize_error`, `sanitize_feed_label`, and `sources/base.py::_safe_url`
run over arbitrary failure text, so both `urlsplit` and `parsed.port` are
guarded: a malformed URL must never abort a run.

---

## 15. Health alert delivery

Alert delivery splits `degraded` by impact **without** changing the state, its
diagnostics, or its history. It is an alert-delivery policy over the existing
`DirectSourceDiagnostics` reason codes — never a second diagnostics,
persistence, or pagination system.

### Severity routing

Severity routes delivery; `alert_type` stays the semantic label.

- **HIGH** is the only severity that emails immediately.
- **MEDIUM** and **INFO** always report in the daily digest.
- There is **no CRITICAL**; `both_tiers_unavailable` is HIGH.
- Recoveries are INFO, so there is no immediate recovery email and no
  unsent-recovery retry path.

### Minor degradation

A degradation is minor only when the direct source is `degraded`, is **not**
truncated, and every reason code is `schema_invalid_records_skipped`,
`malformed_records_skipped`, or `request_retry_recovered`. Skipping stays minor
only when malformed plus schema-invalid counts total at most five **and** at
least twenty rows were retained per skipped record; a recovered retry stays
minor only when the final attempt is `complete` and not `incomplete`.
Classification reads reason codes rather than the `incomplete`/`complete` flags
for skip cases, because skipped records set those flags themselves and every
genuinely partial cause publishes its own code.

Unknown codes, mixed sets, pagination loss or truncation, repeated pages,
pagination request/schema failure, material enrichment failure, substantial
record loss, direct failure, and lost coverage all stay actionable and keep
their immediate alerts.

Minor incidents become `minor_degradation` candidates, and a source returning to
healthy with no open actionable incident becomes `minor_recovery`. Both are
recorded in the alert event log. Minor incidents keep `degraded` state and full
diagnostics, send **no** immediate degradation or recovery email in any mode,
and appear only in the daily digest.

### GitHub-fallback downgrade of a first failure

A **first** direct failure (`consecutive_failures == 1`) is MEDIUM only when
GitHub is a *usable* fallback for that company: a GitHub feed succeeded on this
run, that feed is non-stale under `feed_stale_hours` and has published at least
once, GitHub is not the company's primary source (`bespoke`/`github_only` never
downgrade), and there is a positive company-level GitHub row count on this run or
within the seven-day persisted horizon. Zero and absent counts are unproven, so
a cold start stays HIGH. `github_backstop_available` is the coarse global "some
feed succeeded somewhere" signal and never downgrades anything by itself.

**Two or more consecutive failures are always HIGH.** Fallback delays one alert
and never suppresses escalation; coverage-loss conditions keep their HIGH.

Coverage snapshots are the evidence store: per company `{state, github_rows}`,
with legacy bare-string entries still readable and 200 snapshots retained to
outlast seven days of hourly runs. Row counts come from this run's parsed GitHub
rows matched through the watchlist key; ambiguous labels count for nobody.

### Flapping deferral

An isolated failure also defers to the digest as MEDIUM once the same health key
has at least `FLAP_REPEAT_THRESHOLD` (3) prior failures of the same sanitized
error kind within `FLAP_LOOKBACK_HOURS` (168), and only while GitHub fallback
posture has not weakened against the most recent qualifying occurrence.

The rule keys on `error_kind`, **never** the fingerprint (which omits it). First
failures, second consecutive failures, materially different error kinds, and
regressed fallback posture all stay immediately HIGH. There is no
company-specific logic and no rolling failure-rate escalation. Repeat history is
derived from existing `source_health_alert_events` payloads and read before this
run records its own events; alert-state schema, cooldown semantics,
`resolved_at` resolution, recovery routing, and D1 fallback evidence are
unchanged.

### Systemic grouping

Presentation only, applied **after** per-company incidents are calculated and
persisted: within one run, five or more direct failures in one adapter family
sharing a sanitized error kind that holds at least 60% of that family's failures
render as one grouped HIGH section naming every affected company. No
cross-family grouping, no synthetic health record; every company keeps its own
state, counters, diagnostics, coverage, and cooldown.

### Daily digest

The digest reuses `HealthAlertStore` in `seen.sqlite`. It sends at most once per
UTC day at or after `WATCHER_HEALTH_EMAIL_HOUR_UTC`, and **only a successful
send marks the day**, so a failed digest retries later the same day and an empty
window sends nothing. Each health key collapses to one entry; events up to the
last HIGH are dropped so an escalated incident leaves the digest instead of
reappearing unresolved, while a recovery after that HIGH still reports. Catch-up
resumes at the last successful digest, clamped to seven days with a warning when
the clamp engages. Event retention is 30 days.

Each digest entry reports the source label, occurrences, retained rows, the
bounded diagnostic summary, reason codes, first and last detection, and whether
it recovered. The digest reuses the health-email configuration and sender, runs
after immediate alerts on every path except `off`, and can neither delay nor
suppress them.

### Isolation from internship mail

Health-alert fingerprints and cooldowns use dedicated tables, never `seen`.
Health SMTP is configured independently and has its own renderer and send call.
It can never affect match-email delivery, and it never updates internship
`emailed_at` or `primed_at` or includes alumni data. Zero internship matches
still send no internship email.

Configuration is documented in [`operations.md`](operations.md).

---

## 16. Posting audit and source comparison

`python -m watcher.audit` explains each watcher stage without loading alumni
data. **Audits are read-only** and reuse production identity, dedupe,
classification, eligibility, scoring, and seen lookup.

- The safe default is state-only: it reads the latest sanitized comparison
  snapshot and notification records and makes **no** network requests.
- `--live` performs normal collection, dedupe, analysis, classification,
  eligibility, scoring, and identity decisions but never sends email, primes
  postings, writes seen rows, persists source-health attempts, or requires
  alumni data.

The trace independently reports collection/provenance, watchlist matching,
identity, deduplication and its exact merge tier, season, internship/open and
U.S. status, role confidence/evidence, watcher eligibility, scoring, historical
notification state, and one stable final reason. `--json` emits stable JSON.
Queries accept configured company names or aliases, partial titles, exact or
normalized URLs, native requisition IDs, analyzed job IDs, or canonical identity
keys; `--limit 25` bounds ambiguous results.

### Coverage audit

`--coverage` is a separate offline, read-only report projecting the configured
cohort and the latest persisted `source_health_current` rows into one mutually
exclusive state per company, without collecting jobs, making requests, sending
email, or modifying SQLite. `--coverage --json` emits schema-versioned,
deterministically sorted JSON; `--watchlist` keeps the logic reusable for other
cohorts.

1. `direct_verified` — the current adapter-specific health key is persisted as
   `healthy_with_listings` or `healthy_empty` (legacy successful
   `healthy`/`empty` rows remain trustworthy).
2. `direct_degraded` — a direct source is configured with persisted health, but
   that status is degraded, failed, unknown, or otherwise not a trustworthy
   success.
3. `backstop_only` — `bespoke`/`github_only` intentionally has no direct
   collection and at least one GitHub backstop is configured. Feed health and
   listing counts never prove company-level availability.
4. `no_source_found` — the company explicitly declares
   `coverage_status: no_source_found` after investigation.
5. `needs_investigation` — no persisted adapter-specific health exists for a
   configured direct source, or a no-direct-source entry has no configured
   backstop and no explicit investigation result.

Direct coverage is `direct_verified / total`; accounted coverage is everything
except `needs_investigation`. Optional bounded `platform_family` metadata groups
platform gaps, with unspecified `bespoke` entries in a deterministic catch-all.
**Missing persisted health must stay visible as `needs_investigation` and is
never inferred from configured ATS metadata**, and a configured ATS alone is
never verified coverage.

### Bounded source comparison

Observability only: it cannot affect matching, email, seen state, scoring,
eligibility, collection, or deduplication. Fixed pipeline:

```
all jobs -> lightweight outcomes -> complete counts ->
deterministic detail selection -> rich traces for selected entries ->
persistence/rendering
```

`audit_trace.py` owns one immutable evaluated posting outcome and is the sole
implementation of source sightings, identity, watchlist, internship/open,
season, location, role/watcher eligibility, notification state, final reason,
and merge/anomaly diagnostics. Lightweight `PostingComparisonSummary` values hold
only scalar count/selection data plus an original job index; rich
`PostingAuditTrace` values consume the same outcome and never reevaluate
business rules.

When watchlist, season, internship, or open-state precedence already determines
the lightweight final reason, the shared outcome **defers** location/eligibility
and seen-notification expansion until a detail is selected, then completes those
fields through the same evaluator before serialization. Open candidates still
evaluate eligibility and relevant notification state while producing their
lightweight final reason.

The full job universe is indexed once for generic URLs, seen records, similar
requisitions, duplicate sightings, and cross-source merge relationships. Counts
and aggregates use every lightweight summary, so category counts always cover
the complete posting universe.

**The report builder owns retention policy**; the store persists the provided
ordered entries without a second policy pass and may apply only a defensive hard
maximum. The builder retains all eligible comparisons, no-posting coverage,
non-routine rejections, and operational anomalies, deterministically samples 25
routine rejections per reason with the established SHA-256 key, and applies the
2,000-entry hard cap with stable display ordering.

Schema-version-2 reports keep `entries` for selected rich details and add
`postings_evaluated` and `detail_entries_retained`; schema-version-1 persisted
reports remain readable. SQLite retains exact aggregates for **30 runs** and
selected details for the newest **three runs**. Retention cleanup runs
transactionally with the comparison save and never touches notification or
source-health tables. `VACUUM` is not an hourly default: it runs only after
cleanup deletes at least 500 detail rows and at least 25% of database pages are
free.

---

## 17. Timing logs

Normal runs emit stable INFO records measured with `time.perf_counter()`,
emitted from `finally` blocks so failed fetches are included.

- One `SOURCE-TIMING` line per attempted direct or GitHub fetch, with a
  sanitized company/adapter identifier, success, three-decimal elapsed seconds,
  and returned-row count. GitHub feed records use `company=all` plus the
  configured source name. Adapters exposing request/retry diagnostics
  (currently Workday) add `requests` and `retries`.
- `STAGE-TIMING` records for configuration/startup, direct and GitHub
  collection, total collection, health persistence, analysis,
  filtering/eligibility, alumni work, seen partitioning, digest/email handling,
  source-comparison work, health-alert evaluation, and total runtime.

Identifiers are URL- and secret-free, and records never contain feed URLs,
response content, secrets, alumni details, or recipients. Timing is log-only:
heartbeat and health-report schemas are unchanged.

---

## 18. Standing constraints

- Additive only. No change to scoring, classification, salary, or signal logic.
- Reuse centralized posting identity, `norm_company`, `norm_url`, and
  `analyze_rows` verbatim. Never invent a second scoring or notification
  identity scheme.
- Every external fetch is defensive: time out, catch, log, continue. One bad
  source never blocks the others or the email.
- Respect robots and rate limits; space out requests. Prefer official JSON
  endpoints over HTML scraping wherever both exist.
- No secrets in the repo. No silent failures.
- Never commit `.env`, credentials, alumni data, SQLite state, probe/health
  output, downloaded or extracted Actions diagnostics, snapshots, profiler data,
  generated benchmark reports, or `evaluation/private/`. Reusable helper
  scripts, benchmark tooling, tests, and fixtures stay tracked and reviewable.
