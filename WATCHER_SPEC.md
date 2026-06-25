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
  terms: ["Summer 2026"]          # internship terms we care about
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

### Bespoke (`sources/bespoke/*.py`)

Google, Amazon, Bloomberg, etc. run custom sites. One module each, highest
value (alumni cluster there) but custom and fragile. Several expose an internal
JSON search endpoint — prefer that over HTML parsing. If a site uses Cloudflare
or other anti-bot challenges, do **not** escalate to headless-browser evasion;
mark the company `github_only` in a comment and let Tier 2 cover it. Document
the decision in the module.

### Tier-2 backstop (`sources/github_listings.py`)

Single GET of the raw listings file:
`https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json`

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
Tag every row `source="github"`. Treat the schema defensively: if the file
fails to parse or required keys vanish, log a loud warning and continue with
Tier-1 results — never crash the run.

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
