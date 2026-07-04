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
