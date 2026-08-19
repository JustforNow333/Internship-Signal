# Internship Signal

Separate real engineering internships from busywork. Paste or upload a messy CSV
of postings; get back a cleaned, deduplicated, scored, and flagged board with a
plain-English explanation for every number.

A scheduled **watcher** does the same thing continuously: it collects postings
from a fixed list of companies (direct ATS APIs plus GitHub community feeds),
scores them with the identical engine, and emails a digest of genuinely new
matches annotated with the alumni you know there.

Built for a CS student profile (backend/data/ML-leaning, Flask + SQLAlchemy
experience, Cornell, prefers paid roles with real ownership) — the profile is a
JSON file you can edit, not a hardcoded assumption.

The local app runs entirely offline. No external APIs, no LLM calls, no
telemetry.

---

## Documentation map

| Read this | For |
|---|---|
| [`AGENTS.md`](AGENTS.md) | **Start here for any code change.** Repository map, non-negotiable rules, task routing, test commands. |
| [`docs/architecture.md`](docs/architecture.md) | Component boundaries, data flow, scoring model, classification, ask engine |
| [`docs/watcher.md`](docs/watcher.md) | Watcher behavior: collection, identity, eligibility, notification, cache, snapshots, health, alerts |
| [`docs/watcher-sources.md`](docs/watcher-sources.md) | Per-platform ATS adapter contracts |
| [`docs/operations.md`](docs/operations.md) | GitHub Actions, environment variables, state persistence, probes, rollover, recovery |
| [`docs/testing.md`](docs/testing.md) | Which tests to run, full-suite commands, benchmarks |
| [`evaluation/README.md`](evaluation/README.md) | Scoring benchmark methodology and metrics |
| [`backend/HOSTED_BACKEND.md`](backend/HOSTED_BACKEND.md) · [`frontend/HOSTED_API.md`](frontend/HOSTED_API.md) | Hosted multi-user backend and API contract |
| [`BRANCH_STRATEGY.md`](BRANCH_STRATEGY.md) | Branch ownership and shared-fix transfer policy |
| [`docs/history/`](docs/history/) | Historical implementation log — **not** current behavior |

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
own CSV). The Vite dev server proxies `/api/*` to `localhost:8000`, so there is
no CORS or URL configuration in normal use. `.env.example` documents the few
overridable settings.

For persistent multi-user accounts, HTTP-only sessions, preferences, and
per-user watchlists, follow [`backend/HOSTED_BACKEND.md`](backend/HOSTED_BACKEND.md).

**3. Run the tests**

```bash
cd backend && python3 -m pytest tests/ -q
cd frontend && npm test
```

See [`docs/testing.md`](docs/testing.md) for targeted suites and the checked-in
Windows virtualenv commands.

**4. Run the watcher locally (dry run — no email, no state change)**

```bash
WATCHER_SEND_EMAIL=0 PYTHONPATH=.:backend python3 -m watcher.run \
  --seen-db "$(mktemp --suffix=.sqlite)"
```

Companies and feeds are configured in `watcher/watchlist.yml`. See
[`docs/operations.md`](docs/operations.md) for scheduled operation.

---

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
   `$3,000 for the summer`, `₹1.5L–₹2.4L` all normalize to a USD/hour range with
   a confidence score and explicit notes for every assumption.
5. **Classify** — company type (tech / startup / non-tech / unknown) and role
   (SWE / DS / ML-AI / quant / product / IT / non-technical / unknown), each with
   confidence and the evidence used. The company classifier is layered, not
   name-matching — see [`docs/architecture.md`](docs/architecture.md).
6. **Flag** — red flags (unpaid, equity-only, commission-only, pay-to-work scams,
   "no interview" hiring, WhatsApp recruiting, 3+ years required for an
   internship, founder-responsibility dumping, 10+ tool laundry lists, grunt work
   with no learning) and positive signals (stack match against your profile, pay
   level, ownership, mentorship, conversion path, reputable employer, concrete
   tech stack, backend focus, startup environment).
7. **Score** — transparent 0–100 across eight weighted categories, plus hard
   rules, top reasons, top concerns, and a recommended action (apply now / apply
   later / research more / skip). Weights and rules are in
   [`docs/architecture.md`](docs/architecture.md).
8. **Ask** — a natural-language box answered by a deterministic query
   interpreter, with an explicit LLM integration seam that keeps answers grounded
   in the actual rows.

The watcher adds collection, U.S.-location and student-status eligibility gates,
posting identity, notification memory, and source health on top of the same
engine.

---

## The sample dataset

`data/sample_postings.csv` — 31 rows, 29 unique. Intentionally messy: dirty
headers, an exact duplicate, a near-duplicate (case/whitespace + `utm_` URL),
blank fields to infer, eight-plus pay formats, INR salaries, an unpaid
"exposure" role, an equity-only founder-dump, a commission-only cold-calling
role, a $99-fee WhatsApp scam, a data-entry role disguised by an "Analytics"
employer name, ambiguous company names (Meridian, Kite, Orchid), and one expired
deadline. Expected result with the bundled profile: 16 high / 5 maybe / 8 low,
2 duplicates merged.

The sample's deadlines were written relative to June 2026, so the backend tests
pin `today = 2026-06-09` to stay deterministic. The live app always uses the real
current date.

---

## UX touches

- **Cleaning report** — exactly which columns mapped where, what collided, which
  rows merged (and which fields were filled), what was inferred, and how many
  salaries parsed vs. needed assumptions.
- **Signal bar** — the same horizontal score meter everywhere (table, drawer,
  board), with click-to-explain per-category bars and visible weights.
- **Profile-match chips** — the exact skills and interests that overlapped.
- **Confidence dots** on every inferred verdict (role, company type, salary
  parse), with the evidence one click away.
- **Shortlist + export** — star postings (persists across sessions), then export
  exactly the filtered view as a clean CSV.
- **Action board** — postings grouped by apply-now / apply-later / research /
  skip, with days-left or the top concern on each card.
- **Ask interpretation echo** — every answer shows how the question was parsed
  and which filters ran.

---

## Tradeoffs & limitations (deliberate)

- **In-memory store** for the local app — datasets vanish on backend restart.
  Right for a local tool; swapping in SQLite is a change confined to `store.py`.
  (The watcher and hosted mode do persist state.)
- **Regex classifiers** — fast, explainable, testable; they will misread
  genuinely novel phrasing. Confidence scores and evidence make the misses
  visible instead of silent.
- **Rough FX + conventions** — static currency table; INR lakh amounts without a
  period are read as per-annum (LPA convention) and labeled as such.
- **Client-side filtering** — instant for hundreds of rows; thousands would want
  server-side pagination.
- **Direct ATS coverage is uneven** — some employers are only reachable through
  the GitHub backstops, and that is tracked explicitly as source health rather
  than hidden.

---

## What I'd improve next

1. SQLite persistence + dataset history ("compare this week's scrape to last").
2. Optional LLM behind `interpret()` (the seam already exists) with the
   deterministic engine as fallback and for answer verification.
3. Per-field weight editor in the UI writing back to `profile.json`.
4. Browser-extension or paste-a-URL ingestion to skip the CSV step.
5. Embedding-based dedupe for same-role-different-wording postings.
