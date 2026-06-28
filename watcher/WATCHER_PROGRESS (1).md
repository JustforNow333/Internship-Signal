# Internship Watcher — progress & handoff

## What this project is

A scheduled bot built on top of an existing app ("Internship Signal" — FastAPI
backend + React frontend that scores internship postings). The watcher watches a
curated list of companies for new SWE-internship postings, scores them with the
existing engine, and (soon) emails new matches annotated with which fraternity
alumni work at that company. Direct ATS scraping is the primary "first-wave"
source; the SimplifyJobs GitHub listings file is the backstop. Goal: catch
nearly every intern-hiring company on the user's target list, fast, with a
referral contact attached.

The full build spec is `WATCHER_SPEC.md` in the repo root — the authoritative
reference. This file is the running status.

## Architecture (unchanged, for context)

```
Scheduler (GitHub Actions, hourly)
  -> for each watchlist company: direct ATS adapter (first-wave)
  -> always: GitHub listings.json backstop
  -> merge -> analyze_rows() [existing scoring engine] -> SWE/intern/open filter
  -> seen-store (dedupe by content-hash job_id) -> alumni join -> email digest
```

Key reuse principle: the watcher never computes its own score or job id. It
produces canonical-shaped row dicts (keys = `CANONICAL_COLUMNS`) and feeds them
to the existing `analyze_rows()`; everything downstream (dedupe, classify, flag,
score) is the engine that already exists and is tested.

## DONE so far (committed work, in order)

1. **`analyze_rows` refactor** — extracted the per-row analyze+score loop out of
   `process_csv` into a reusable `analyze_rows(rows, today)` in
   `backend/app/ingest.py`. Behavior-preserving; all original 86 backend tests
   still pass. This is the seam the whole watcher plugs into.

2. **Source adapter foundation + first adapters** — `watcher/` package created.
   `sources/base.py` defines the `Source` protocol, error types
   (`SourceError`, `SourceFetchError`, `SourceSchemaError`), and shared helpers
   (`make_row`, `fetch_json`, `post_json`, `html_to_text`, `iso_date`,
   `ensure_list`, `require_token`). Built Greenhouse + Lever adapters and the
   GitHub `listings.json` backstop, each with offline fixture tests.

3. **Encoding hardening** — fixed a Windows mojibake bug (a fixture read as
   cp1252 instead of UTF-8) and audited the whole codebase; all file I/O now
   uses explicit `encoding="utf-8"`. Regression test added. (Root cause was the
   saved fixture, not the live fetch path — live fetch was always correct.)

4. **Watcher core (the end-to-end milestone)** — `seen_store.py` (SQLite, keyed
   on the existing content-hash `job_id`), `filters.py` (SWE-role-only +
   internship + open, applied after scoring, role set is a config constant),
   merge with direct-source-priority, and a print-only `run.py`. Verified: a
   live run prints new SWE-intern matches; a second run prints nothing (seen-
   store works). Confirmed a GitHub-only posting (blank description) still
   classifies as SWE on title alone but scores lower — expected thin-data
   behavior.

5. **`detect.py` + starter watchlist** — a helper that resolves each company's
   ATS + token from its live careers-page URL. Ran it across the 61 Tier-1
   companies. Output: **29 resolved**, **23 bespoke/unsupported-platform**,
   **9 unresolved**. Generated `watcher/watchlist.yml` (unresolved -> `github_only`,
   bespoke -> `bespoke`). Anduril validation case passed
   (`greenhouse/andurilindustries` — the spec's original Lever guess was wrong).
   Also corrected a data typo it surfaced: "Procutre Analytics" is really
   "Procure Analytics".

6. **Remaining ATS adapters + dispatch (just finished)** — built Workday, Ashby,
   SmartRecruiters, Workable adapters + `post_json()` helper, fixture tests for
   each, `workday_shard` support in config/detect/watchlist, run-loop dispatch by
   `ats` value, and direct-fetch now skips `bespoke`/`github_only`. Live
   end-to-end verified: Capital One (Workday) 1264 scored jobs, OpenAI (Ashby)
   719, LinkedIn (SmartRecruiters) 248. **Full suite: 127 passed, 1 warning.**

## Current state of the data files

- `watcher/alumni.csv` — the full 332-person roster (NOT just the 71), cleaned:
  one junk employer fixed (Spencer Weiss "Google (I WILL BE REACHING OUT...)" ->
  "Google"), byte-identical name+employer dupes dropped, honest typos left in for
  the fuzzy matcher to handle. 313 have LinkedIn URLs. **Must be gitignored**
  (real personal data). A committed `alumni.example.csv` with fake rows is still
  TODO if not yet created.
- `watcher/watchlist.yml` — all 61 Tier-1 companies; 29 with real ATS+token,
  rest marked bespoke/github_only.
- Decision settled: the alumni file stays UNFILTERED (full roster). The
  watchlist is the curated thing. Rationale: an alum only ever surfaces when
  their company posts a watched tech job, so filtering alumni can only wrongly
  exclude a useful contact. The "does this company have tech jobs" filter happens
  automatically at match time.

## KNOWN ISSUES / things to watch

- **Six Workday companies return maintenance/refresh HTML, not JSON**: Workday
  (self), PayPal, Morgan Stanley, Adobe, IQVIA, Northrop Grumman. The adapter
  fails loudly (good) rather than returning empty. Capital One works. This needs
  investigation in the next session — likely a session/cookie or endpoint-path
  nuance per tenant, OR these tenants genuinely gate the CXS endpoint. Until
  fixed, those six effectively fall back to the GitHub backstop.
- **The "bespoke" pile is mixed.** Some are truly custom (Google, Amazon,
  Bloomberg, IBM, Oracle, Uber), but several are standard platforms the adapters
  just don't support yet: **iCIMS** (Arm, JHU APL, BlackLine, ZS), **Eightfold**
  (American Express), **SuccessFactors** (MIT Lincoln Lab), **Paylocity** (Procure
  Analytics), **Pinpoint** (Coursedog). Adding an iCIMS adapter especially would
  reclaim several real targets (Arm has 3 alumni). Lower priority than email.
- **GitHub-only postings score lower** (blank description/requirements). When the
  email digest gets a score gate, keep it low or off so thin backstop rows aren't
  silently dropped.
- **"Compensation unclear" fires on nearly every posting** (neither Greenhouse
  nor GitHub gives structured pay) — it's noise in this context; consider
  de-emphasizing it in the digest rather than showing it as a red flag every time.

## WHAT'S LEFT (build order going forward)

1. **Investigate the 6 Workday maintenance-HTML failures** — get PayPal/Morgan
   Stanley/Adobe/IQVIA/Northrop Grumman/Workday returning JSON, or confirm they
   genuinely require the backstop. (Capital One already works, so the adapter is
   fundamentally correct — this is per-tenant.)
2. **Alumni join into the output** (`watcher/alumni.py`) — load `alumni.csv`,
   index employers via the existing `dedupe.norm_company`, match each posting's
   company to alumni with layered exact -> alias -> conservative-fuzzy matching,
   LOG fuzzy matches (the typos like "Capitol One", "Chainanalysis" are the test),
   and attach name/occupation/LinkedIn to each match. Create the committed
   `alumni.example.csv`. Offline tests with a FAKE fixture alumni file.
3. **Email digest** (`watcher/notify.py`) — one digest per run via stdlib
   `smtplib` (Gmail app password), each posting showing score + action + top
   reason + red flags + apply URL + source tag + the alumni you know there.
   Secrets via env only. Send nothing if zero new matches.
4. **GitHub Actions scheduler** (`.github/workflows/watcher.yml`) — hourly cron +
   manual dispatch. Must persist the SQLite seen-store across ephemeral runs
   (recommended: commit it back to a data branch, or use an artifact). Reproduce
   the `PYTHONPATH=.:backend` wiring in CI. Surface partial failures loudly (a
   heartbeat line: "ran, N new, M errors") so a silently-broken scraper is
   visible. Secrets via GitHub Actions secrets.
5. **(Optional, later) iCIMS adapter** to reclaim Arm + the lab/enterprise
   bespoke pile.

## Working conventions that have served well

- One scoped session per step; commit between steps for clean rollback.
- Reference `WATCHER_SPEC.md` by filename in each prompt; don't paste it.
- Tell the agent to STOP at the named step (it has momentum to overrun).
- For any adapter/network work: verify endpoints LIVE (don't trust spec/memory —
  this caught Anduril and the Workday shard issues), and NO network in tests
  (saved fixtures only).
- Demand real test output pasted, never a claim of passing.
- Test command in use:
  `PYTHONPATH=.:backend backend/venv/Scripts/python.exe -m pytest backend/tests watcher/tests -q`
  (Windows venv path; current suite = 127 passed.)

## Quick orientation prompt for the new chat

> Read `WATCHER_SPEC.md` and `WATCHER_PROGRESS.md` in the repo root. The watcher
> core, all six ATS adapters, detect.py, the watchlist, and the cleaned alumni.csv
> are done and committed (suite: 127 passed). Per the build order in
> WATCHER_PROGRESS.md, the next step is [the alumni join / the Workday HTML fix /
> the email digest]. Do only that step, verify endpoints live and keep tests
> offline, paste real test output, and stop when done.
