# Internship Signal

Separate real engineering internships from busywork.

The repository is three things that share one analysis engine:

| Part | What it is |
|---|---|
| **Analyzer** | Paste or upload a messy CSV; get a cleaned, deduplicated, scored board with a plain-English reason for every number. Runs locally. |
| **Hosted app** (`product-mvp`) | Multi-user accounts, preferences, per-user company watchlists, durable matches, and email digests on PostgreSQL. |
| **Watcher** | A scheduled bot that reads **340 companies'** official careers sources, scores new postings with the same engine, and emails what is genuinely new. |

Scoring is deterministic: no LLM calls anywhere in the pipeline. The analyzer
needs no network at all; the watcher fetches public careers endpoints and the
hosted app talks to PostgreSQL and SMTP.

The candidate profile (skills, interests, locations, pay floor) is
`data/profile.json` — an editable file, not a hardcoded assumption.

---

## Quickstart

Requirements: Python 3.10+, Node 18+.

```bash
# Backend — FastAPI on :8000
pip install -r backend/requirements.txt
PYTHONPATH=.:backend uvicorn app.main:app --reload --port 8000

# Frontend — Vite + React on :5173
cd frontend && npm install && npm run dev
```

Open http://localhost:5173 and click **load the sample dataset**, or drop in a
CSV. Vite proxies `/api/*` to port 8000, so there is no CORS or URL setup.
`.env.example` lists the overridable settings.

For accounts, sessions, watchlists, and digests, see
[backend/HOSTED_BACKEND.md](backend/HOSTED_BACKEND.md).

---

## Coverage

340 configured companies:

| Tier | Count | Meaning |
|---|---:|---|
| Direct-complete | 269 | A first-party source whose full inventory is provably enumerable |
| Direct practical-partial | 4 | An official but deliberately bounded slice (Alibaba, Ansys, Huawei, Siemens) |
| Backstop | 67 | No defensible direct contract; covered by typed GitHub feeds (50) or bespoke pages (17) |

38 direct ATS adapters are registered in `watcher/sources/registry.py`, which is
the single source of truth for which direct sources exist and how they are
built. Adding one usually also needs a package export, config validation, a
concurrency origin, a watchlist entry, and tests.

**Direct-complete is a claim with a standard behind it.** An adapter may only
report `complete=true` when it can prove exact totals, page termination,
stable posting identity, canonical URLs, and observable failure behavior, and
when two consecutive snapshots agree. Anything less fails closed, or registers
as practical-partial and permanently publishes `complete=false`. Counts are
never inferred from "the payload looked big."

Company coverage is layered: the original 251-company recognizable-tech
universe, plus later expansion universes (finance employers, and so on).
Expansion batches only append; they never reach into an earlier milestone, and
`watcher/tests/tech_universe.py` pins that by membership.

---

## What it does

1. **Ingest & clean** — sniff the delimiter, normalize messy headers
   (`"Pay"`, `" Job Title "`, `"Remote?"` → canonical columns), strip nullish
   cells, fix unicode dashes/NBSPs, and report every unmapped or colliding
   column rather than dropping it silently.
2. **Dedupe** — collapse exact and near duplicates (case/whitespace, `utm_*`
   URL variants), filling fields the kept row was missing, with a report line
   per merge.
3. **Infer** — fill derivable blanks (remote status, location, term) and label
   what was inferred.
4. **Parse compensation** — `$25/hr`, `$4k/month`, `80k`, `₹1.5L–₹2.4L` all
   normalize to a USD/hour range with a confidence score and explicit notes for
   every assumption.
5. **Classify** — company type and role, each with confidence and evidence.
6. **Flag** — red flags (unpaid, equity-only, pay-to-work scams, 3+ years
   required for an internship) and positive signals (stack match, ownership,
   mentorship, conversion path).
7. **Score** — 0–100 across eight weighted categories, with top reasons, top
   concerns, and a recommended action.
8. **Ask** — a natural-language box answered by a deterministic interpreter.

---

## Scoring

`score = Σ (category_score × weight)`, then hard rules. Weights live in
`backend/app/config.py` and sum to 1.00:

| Category | Weight | Measures |
|---|---:|---|
| role_relevance | 0.40 | Role type × the profile's role affinities |
| compensation | 0.14 | USD/hr band; unpaid = 0 |
| learning_value | 0.14 | Mentorship, ownership, structured program, conversion |
| technical_depth | 0.14 | Concrete tools named; capped low for non-technical roles |
| legitimacy | 0.08 | Starts at 70; −30 critical, −12 major, −4 minor per flag |
| effort_vs_value | 0.04 | Application hoops vs. what you get |
| location_convenience | 0.03 | Remote, or near preferred locations |
| deadline_urgency | 0.03 | Time pressure; expired = 0 |

Hard rules, applied after the weighted sum:

- Outside the SWE watcher track ⇒ capped at 44, bucket `low`, action `skip`.
- Any **critical** flag (e.g. asks applicants to pay) ⇒ capped at 40, `low`,
  `skip` — headline pay cannot rescue a scam.
- **Three or more major flags** ⇒ capped at 44, `low`, `skip` — a pattern, not
  a coincidence.
- Expired deadline ⇒ `skip` regardless of score.
- Otherwise: ≥ 75 `apply_now`, ≥ 45 `apply_later`, else `research_more`.

Buckets: **high ≥ 70**, **maybe 45–69**, **low < 45**. Every category returns a
one-line explanation, so any score can be audited by clicking it.

Benchmarks, including the clean-commit independent U.S. holdout workflow, are
measurement-only and live in [evaluation/README.md](evaluation/README.md).

---

## Watcher

Each run collects every configured company, merges rows by fixed source
precedence (direct ATS → Simplify JSON → configured Markdown tables), scores
them with the analyzer engine, filters to open SWE internships, joins alumni
context, and emails only postings not seen before.

Three explicit notification modes:

- **Live send** — `WATCHER_SEND_EMAIL=1`. Matches are emailed, and only then
  marked `emailed_at`. SMTP failure leaves every posting pending.
- **Dry run** — email disabled, no `--prime-seen`. Nothing is written to the
  seen store.
- **Prime** — email disabled plus `--prime-seen`. Current matches are
  suppressed as `primed_at` without invoking email.

`send_email` and `prime_seen` together are rejected.

```bash
python3 -m watcher.run                                   # collect, score, notify
python3 -m watcher.audit --coverage                      # per-company coverage audit
python3 -m watcher.audit --company Google                # why one company produced what it did
python3 -m watcher.audit --url "https://…/jobs/300697/"  # trace one posting end to end
```

Season terms, GitHub backstop feeds, and the rollover procedure are configured
in `watcher/watchlist.yml`. The run reports a season status (`ok`,
`rollover_due`, `stale`, `unknown`) and warns without halting collection.

Per-source contracts, completeness rules, concurrency, snapshot/replay, and
health semantics are specified in [WATCHER_SPEC.md](WATCHER_SPEC.md); that file,
not this one, is the authority.

---

## Ask the dataset

`backend/app/ask.py` splits the feature in two:

- `interpret(question) -> QueryPlan` — keyword/regex rules producing a small,
  inspectable plan.
- `run_plan(plan, jobs) -> answer` — pure filtering and ranking over scored rows.

Every answer echoes how the question was parsed and which filters ran.
**LLM integration point:** replace only `interpret()` (the seam is marked in the
file). `run_plan` stays deterministic, so answers remain grounded in real rows.

---

## Architecture

```
backend/app/
  main.py       FastAPI routes (ingest, jobs, summary, ask, profile)
  ingest.py     pipeline orchestration + cleaning report
  normalize.py  header mapping, cell cleaning, inference
  dedupe.py     canonical keys, URL normalization, merge report
  salary.py     compensation parser → USD/hr + confidence
  classify.py   layered company + role classifiers
  signals.py    red flags, positive signals, profile match
  scoring.py    weighted categories + hard rules + actions
  ask.py        interpret() / run_plan()
  hosted/       accounts, sessions, matches, digests (PostgreSQL)
watcher/
  sources/      one module per ATS; registry.py owns membership
  config/       watchlist loading + per-ATS validation
  health/       coverage, transitions, reports
  watchlist.yml the 340 companies and season config
frontend/src/   React app; hosted/ holds the multi-user UI
data/           sample CSV, known companies, profile
```

CSV → normalize → dedupe → per row (parse comp → classify → flag → score) →
summary. Job ids are stable content hashes, so a saved shortlist survives
re-ingesting the same file.

---

## Tests

```bash
PYTHONPATH=.:backend python3 -m pytest backend/tests -q   # 416 passed, 100 skipped
PYTHONPATH=.:backend python3 -m pytest watcher/tests -q   # 3225 passed
cd frontend && npm test                                   # vitest
PYTHONPATH=.:backend python3 -m compileall -q internship_signal backend watcher scripts
```

The 100 skips are hosted PostgreSQL tests, which need a database URL.

---

## Docs

| File | Purpose |
|---|---|
| [agents.md](agents.md) | The complete repository guide — read first |
| [WATCHER_SPEC.md](WATCHER_SPEC.md) | Watcher contracts and per-source rules |
| [WATCHER_PROGRESS.md](WATCHER_PROGRESS.md) | Dated log of coverage work |
| [backend/HOSTED_BACKEND.md](backend/HOSTED_BACKEND.md) | Hosted storage, auth, phases |
| [frontend/HOSTED_API.md](frontend/HOSTED_API.md) | Hosted API surface used by the UI |
| [evaluation/README.md](evaluation/README.md) | Benchmarks and the holdout workflow |
| [BRANCH_STRATEGY.md](BRANCH_STRATEGY.md) | Branch policy |

---

## Limitations (deliberate)

- **Analyzer datasets are in memory** — they vanish on backend restart. The
  hosted path is where persistence lives.
- **Regex classifiers** — fast, explainable, testable; they will misread novel
  phrasing. Confidence and evidence make misses visible rather than silent.
- **Rough FX** — static currency table; INR lakh amounts without a period are
  read as per-annum and labeled as such.
- **Client-side filtering** — instant for hundreds of rows; thousands would want
  server-side pagination.
- **Phase 3B is unimplemented** — automated watcher execution, scheduled
  imports, and worker scheduling are not wired up.

## Next

1. Phase 3B: scheduled hosted imports and worker execution.
2. Optional LLM behind `interpret()`, with the deterministic engine as fallback.
3. Per-field weight editor writing back to `profile.json`.
4. Paste-a-URL ingestion to skip the CSV step.
5. Embedding-based dedupe for same-role-different-wording postings.
