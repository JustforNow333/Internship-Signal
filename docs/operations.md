# Operations

Running the watcher in production: GitHub Actions, environment configuration,
state persistence, probes, and recovery. Behavior contracts live in
[`watcher.md`](watcher.md); test commands live in [`testing.md`](testing.md).

---

## 1. GitHub Actions workflow

`.github/workflows/watcher.yml` runs hourly (`cron: "0 * * * *"`, best-effort
under GitHub load) plus `workflow_dispatch`.

**Manual dispatch inputs**

| Input | Default | Meaning |
|---|---|---|
| `send_email` | `false` | Send the live digest. Off is a side-effect-free dry run. |
| `prime_seen` | `false` | Explicitly suppress current eligible postings without emailing. |
| `workday_transport_probe` | `false` | Run only the isolated five-tenant Workday probe. |
| `health_email_mode` | `off` | Independent source-health email mode for this run. |

`send_email` and `prime_seen` are independent: an email-disabled run does **not**
imply priming, and live send plus prime is invalid. Scheduled priming uses the
separate optional repository variable `WATCHER_PRIME_SEEN`.

**Scheduled production settings** (workflow `env`, not application defaults):

```yaml
WATCHER_COLLECTION_MODE: "concurrent"
WATCHER_COLLECTION_MAX_WORKERS: "4"
WATCHER_WORKDAY_MAX_CONCURRENCY: "1"
WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY: "2"
```

Rollback is changing only `WATCHER_COLLECTION_MODE` back to `serial`. The
application default remains `serial`, and no additional production behavior is
authorized without a separate reviewed change.

**Scheduled send-mode parsing** accepts `1`, `true`, `yes`, `y`, `on` as true and
`0`, `false`, `no`, `n`, `off` as false, ignoring case and surrounding
whitespace. A missing or blank `WATCHER_SEND_EMAIL` is reported separately from
an explicit false. Any other nonblank value is invalid, emits an Actions warning
naming the variable, and resolves conservatively to false.

**Job summary and heartbeat.** Every normal run starts its job summary with
**Scheduled delivery**, reporting whether delivery is enabled and how many
otherwise-new postings remain pending because it is disabled. A disabled
schedule emits a nonfatal warning even at zero pending; manual dry runs show
`not applicable` and get no warning. The workflow captures the exact last
application `HEARTBEAT:` line and forwards it unchanged before appending
`scheduled_email_enabled`, `pending_due_to_email_disabled`,
`scheduled_email_config`, `seen_loaded`, `seen_saved`, and `seen_store`. A
missing application heartbeat or a corrupt seen store is a hard failure; source
failures remain warnings. Preserve every application heartbeat field — no second
field list may become stale.

Actions also writes the run ID, run counts, health aggregates, seen-store
status, and actionable detail to the summary; warns on newly degraded/failing
transitions and recoveries; emits a nonfatal error annotation per currently
uncovered company; and uploads the sanitized health and source-comparison JSON
reports for 14 days plus the bounded Markdown comparison in the summary.

---

## 2. State persistence

| Store | Location | Lifecycle |
|---|---|---|
| `seen.sqlite` | committed to the orphan `watcher-data` branch | durable: notification, source-health, health-alert, and source-comparison state |
| `analysis-cache.sqlite` | `actions/cache@v4`, daily versioned key with runner OS and `STATIC_ANALYSIS_CACHE_VERSION` | rebuildable; **never** on `watcher-data` |

The workflow points the app at `.watcher-state/seen.sqlite`; the app default is
`watcher/seen.sqlite`, overridable with `WATCHER_SEEN_DB` or `--seen-db`. Load
logs either `SEEN-STORE: bootstrapping empty (no prior data branch)` or
`SEEN-STORE: loaded N seen ids`; a corrupt persisted database fails the job at
load. Save commits and pushes back to `watcher-data`; push rejection triggers a
bounded three-attempt fetch/reset/retry loop and a final push failure is a hard
workflow failure.

Keep the rebuildable cache transactionally independent from the durable
database. Deleting `watcher-data` intentionally resets **both** seen and health
history; the next run initializes directly from the current attempt and emits no
recovery alerts.

Migrate legacy embedded cache rows explicitly, without changing the source:

```bash
PYTHONPATH=.:backend python3 scripts/migrate_analysis_cache.py \
  --source seen.sqlite --destination analysis-cache.sqlite
```

Add `--remove-source-table` only for an intentional cleanup: it creates and
validates a backup, drops only `analysis_cache` and its indexes, vacuums the
source, and revalidates every non-cache table.

---

## 3. Environment configuration

`.env.example` documents the full set. Process environment values win over
dotenv values.

**Notification**

| Variable | Default | Notes |
|---|---|---|
| `WATCHER_SEND_EMAIL` | unset (dry run) | see the mode table in [`watcher.md`](watcher.md#9-digest-and-notification-state) |
| `SMTP_USER`, `SMTP_APP_PASSWORD`, `EMAIL_TO` | — | required for a live send |
| `WATCHER_SEEN_DB` | `watcher/seen.sqlite` | also `--seen-db` |

**Source health mail** (configured independently of internship mail)

| Variable | Default | Notes |
|---|---|---|
| `WATCHER_HEALTH_EMAIL_MODE` | `transitions_only` | `off`, `transitions_only`, `failure_only`, `daily_summary` |
| `WATCHER_HEALTH_EMAIL_HOUR_UTC` | `12` | daily-summary and digest hour |
| `WATCHER_HEALTH_ALERT_COOLDOWN_HOURS` | `24` | repeated-failure cooldown |
| `WATCHER_FEED_STALE_HOURS` | `48` | configured-season feed inactivity threshold |
| `WATCHER_HEALTH_REPORT_PATH` | unset | also `--health-report`; writes the sanitized JSON report |

`transitions_only` sends new failures, recoveries, newly silent productive
direct boards, coverage regressions, and both-tier outages. `failure_only` also
allows continued failures after cooldown. `daily_summary` sends at most once per
UTC day after the configured hour with source-state totals, backstop-only and
uncovered companies, recent transitions, stale feeds, and source-comparison
counts. A structurally valid GitHub fetch with zero matching roles is never a
failure, and stale-feed alerts require prior configured-season activity.

**Collection and caching**

| Variable | Default | Range |
|---|---|---|
| `WATCHER_COLLECTION_MODE` | `serial` | `serial`, `concurrent` |
| `WATCHER_COLLECTION_MAX_WORKERS` | `4` | 1–16 |
| `WATCHER_WORKDAY_MAX_CONCURRENCY` | `1` | 1–5 |
| `WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY` | `2` | 1–4 |
| `WATCHER_WORKDAY_MIN_INTERVAL_SECONDS` | `0.5` | 0–10; `0` disables tenant pacing |
| `WATCHER_ANALYSIS_CACHE_ENABLED` | `true` | `true`/`false`, `yes`/`no`, `on`/`off`, `1`/`0` |
| `WATCHER_ANALYSIS_CACHE_PATH` | beside the seen database | — |

**Alumni**

`WATCHER_COMPANY_ALUMNI_JSON_PATH`, `WATCHER_COMPANY_ALUMNI_JSON`,
`WATCHER_ALUMNI_CSV`, `WATCHER_REQUIRE_ALUMNI`. In Actions prefer the repository
secret `WATCHER_COMPANY_ALUMNI_JSON_B64`.

Backend, hosted, and frontend variables are in `.env.example` and
[`../backend/HOSTED_BACKEND.md`](../backend/HOSTED_BACKEND.md).

---

## 4. Season rollover

To roll from `Summer 2027` to `Summer 2028` **without editing Python**:

1. Find the active repository on the official SimplifyJobs GitHub organization.
   Do not infer a repository name from the year.
2. GET the candidate raw `listings.json` URL and confirm HTTP 200, a top-level
   list, and every entry's required keys: `company_name`, `title`, `locations`,
   `url`, `date_posted`, `active`, `terms`. Confirm `locations` and `terms`
   remain lists and inspect the exact term strings.
3. Verify each configured Markdown URL still returns UTF-8 Markdown with the
   expected five-column internship table. Update its URL and `name` if the
   repository changed, and set `default_term` to the new exact term.
4. Change `defaults.terms` to `["Summer 2028"]`. Keep only feeds verified to
   contain the intended cycle; overlapping feeds may coexist as separate typed
   entries. If no verified next-cycle feed exists for a format, remove only that
   typed entry and rely on the remaining sources.
5. Run the offline backend/watcher tests, then a separate isolated probe:

```bash
probe_db="$(mktemp --suffix=.sqlite)"
probe_report="$(mktemp --suffix=.json)"
WATCHER_SEND_EMAIL=0 PYTHONPATH=.:backend python3 -m watcher.run \
  --seen-db "$probe_db" --health-report "$probe_report"
```

Do **not** add `--prime-seen`. Use an explicit false value rather than unsetting
`WATCHER_SEND_EMAIL`, because the dotenv loader may otherwise restore a local
`.env` send setting. Confirm the report contains one independent successful
attempt per configured feed, `sent=no`, `seen_marked=0`, and zero rows in the
temporary database's `seen` table.

Live endpoint verification is deliberately separate from fixture-based tests.
The Simplify URL was live-verified on 2026-07-15 (its repository name is
historical, but the payload includes the exact `Summer 2027` term); the
`sndsh404` README table was live-verified on 2026-07-24. Both are configuration
values and must be rechecked at each rollover.

---

## 5. Alumni map for Actions

Generate a compact JSON map containing only alumni attached to watchlist
companies. Keep it private and never commit it.

```bash
python scripts/build_watcher_alumni_map.py \
  --csv "C:\path\to\alumni.csv" \
  --watchlist watcher/watchlist.yml \
  --out private/company_alumni.json
```

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("private/company_alumni.json")) | Set-Clipboard
```

Create the repository secret `WATCHER_COMPANY_ALUMNI_JSON_B64` and paste the
value. Rerun the watcher and confirm the log shows something like
`ALUMNI: status=loaded-json-map records=12 employers=8`, and the digest reports
`Alumni index: X records across Y employers`.

---

## 6. Probes and debugging

**Posting audit** (read-only; state-only by default):

```bash
PYTHONPATH=.:backend python3 -m watcher.audit --company Google
PYTHONPATH=.:backend python3 -m watcher.audit --company Uber --title "Software Engineering Intern"
PYTHONPATH=.:backend python3 -m watcher.audit --url "https://jobs.uber.com/en/jobs/300697/"
PYTHONPATH=.:backend python3 -m watcher.audit --requisition-id 300697 --json audit.json
PYTHONPATH=.:backend python3 -m watcher.audit --job-id "<analyzed-id>" --live
```

**Coverage and source comparison:**

```bash
PYTHONPATH=.:backend python3 -m watcher.audit --coverage
PYTHONPATH=.:backend python3 -m watcher.audit --coverage --json
PYTHONPATH=.:backend python3 -m watcher.audit --comparison
PYTHONPATH=.:backend python3 -m watcher.audit --comparison --live \
  --comparison-json source-comparison.json \
  --comparison-markdown source-comparison.md
```

Coverage execution may use the default or an explicit `--seen-db`.

**Workday transport probe** (`scripts/probe_workday_transport.py`) — at most five
tenants; no seen database, alumni data, or SMTP:

```powershell
$env:WATCHER_SEND_EMAIL = "0"
$env:PYTHONPATH = ".;backend"
backend\venv\Scripts\python.exe scripts\probe_workday_transport.py
```

The `workday_transport_probe=true` dispatch input runs the same probe on a
GitHub-hosted runner without restoring or saving `watcher-data`. It prints only
company/shard, attempt count, status, content metadata, body size/hash prefix,
JSON decode state, and jobs-field presence. If GitHub-hosted runners remain
blocked, keep the GitHub backstop active and pursue legitimate Workday access
with the provider — this project never harvests cookies, rotates proxies,
automates browsers, or bypasses challenges.

**Snapshot capture and replay:**

```bash
PYTHONPATH=.:backend python3 -m watcher.run \
  --capture-collection-snapshot watcher/collection-snapshots/latest.json.gz \
  --seen-db .watcher-state/seen.sqlite

PYTHONPATH=.:backend python3 -m watcher.run \
  --replay-collection-snapshot watcher/collection-snapshots/latest.json.gz \
  --seen-db .watcher-state/seen.sqlite
```

**Local SQLite inspection:**

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

## 7. Investigations and audits

- **Coverage audits are offline and read-only.** Trust persisted direct-source
  health, use explicit watchlist metadata for investigation and platform status,
  and never treat global GitHub-feed health as company listing evidence.
- **Fresh coverage baselines** use one isolated real collection at the scheduled
  `4/1/2` limits with internship and health email off, no priming, and the same
  temporary seen database for collection and audit; remove runtime artifacts
  afterward.
- **Coverage-recovery discovery** diagnoses Workday failures as a cluster before
  reaching for tenant exceptions, verifies official careers flows and live
  endpoints, and keeps bounded probes read-only without editing watchlist or
  state. A confirmed direct-source recovery changes only watchlist configuration
  and focused config tests — never widen adapter contracts and never mark health
  verified by hand.
- **Workday schema-skip audits** compare sanitized field shapes across affected
  and healthy tenants and accept variants only with a preserved posting-URL
  contract.
- **IBM stability audits** compare bounded complete passes, job-ID sets, and page
  membership. Never tolerate changing totals or union partial passes; accept only
  a hard-bounded rule that proves a stable complete set.
- **Live recall verification** uses only configured GitHub feeds with temporary
  seen, cache, and health paths, email and health email off, and no priming.
- **Performance audits** distinguish offline pytest time from live watcher time;
  use timing logs, test durations, and isolated profiles before proposing an
  optimization, and keep diagnosis read-only unless a fix is requested.
- Adapter recoveries use existing persisted health state. Never reset
  `watcher-data` or hand-edit a source row to force a recovery.

---

## 8. Concurrency canaries and promotion

Validation is staged, and one benchmark never justifies a change.

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

Stage 1 confirms exact serial/concurrent batch equivalence, exact downstream
fixture equivalence, ordering invariants, concurrency limits, failure isolation,
and clean executor shutdown.

Every canary is collection-only and operationally isolated: temporary seen and
analysis-cache databases; internship email, priming, and seen marking disabled;
health-alert delivery, durable health persistence, and source-comparison
persistence disabled. Production SQLite state is fingerprinted before and after
and the result is reported. A source answering 401, 403, 429, a challenge
response, or repeated transport failures is recorded and **dropped** from the
rest of the canary rather than retried. Full runs are separated by at least one
normal collection interval, and a fresh full serial collection is never run
back-to-back with a concurrent canary — take the serial baseline from a recent
normal production run, existing serial timing logs, or a serial run in a
separate normal collection window.

Before a full canary, record `git status --short --branch`, local `HEAD`, the
upstream ref, a fresh remote branch tip, and the watchlist SHA-256. The
implementation and canary tooling should already be committed and pushed on the
intended branch; if they are uncommitted or unpushed, report the exact state and
do not call the evidence repository-complete. Do not commit or push without
separate authorization.

Use fixed `4/1/2` limits for every promotion canary and store detailed reports
and snapshots only in `evaluation/private/`. Reports separate deterministic
equivalence results, limited-canary results, full-canary results, and the
historical serial baseline, and record for every canary the configured limits,
maximum observed global/per-origin/Workday concurrency, source
successes/empties/failures, HTTP 401/403/429 counts, challenge counts, Workday
request and retry totals, rows per source, unexpected exception count, and
whether production state changed. Quote retained report fields exactly and label
unavailable historical telemetry instead of reconstructing or estimating it.

A canary **fails** when a recently healthy source becomes blocked or challenged,
429s increase, 401/403 newly appear, Workday retries materially increase,
successful-source count materially decreases, per-source row coverage materially
decreases without an explained posting change, an adapter reports an unexpected
exception, required attempts or diagnostics are missing, ordering or dedupe
precedence differs, an executor fails to shut down cleanly, production SQLite
state changes, or any email, priming, or seen-marking path is invoked. Changing
live posting counts are never called a concurrency regression without
source-level evidence.

Promotion review requires three successful full canaries at the same `4/1/2`
limits in separate normal collection windows, complete source and downstream
diagnostics, clean executor shutdown, stable ordering and precedence, comparable
source coverage, no new blocking behavior, and zero production side effects.
Meeting those criteria still does not authorize a change: promotion is a
separate small reversible change.

---

## 9. Rollout verification

Rollout verification dispatches with internship email, priming, the Workday
probe, and health email **all disabled**. Preserve notification timestamps and
restore preexisting repository variables before enabling conservative health
alerts.

After confirming the data branch exists and the heartbeat looks right, set the
repository Actions variable `WATCHER_SEND_EMAIL=true` to enable scheduled sends
and leave `WATCHER_PRIME_SEEN` unset or false for normal operation. Keep
source-health mode and internship-match send mode independently configured; use
`health_email_mode=off` for transport probes.

Use manual `send_email=false, prime_seen=false` for a notification-state-safe
dry run, and `prime_seen=true` only for an intentional one-time suppression.

Inspect the first scheduled production run at fixed `4/1/2`; return only
`WATCHER_COLLECTION_MODE` to `serial` if concurrency introduces new blocking,
reliability, ordering, pacing, shutdown, or diagnostic regressions.
