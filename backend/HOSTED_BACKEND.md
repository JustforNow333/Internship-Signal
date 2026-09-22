# Hosted backend through Phase 3A

Phase 1 adds persistent multi-user accounts, preferences, company watchlists,
verification/reset tokens, and unsupported-company requests. PostgreSQL is the
authoritative datastore. Phase 2A adds central final-job persistence and
idempotent offline snapshot imports. Phase 2B adds durable per-user matching
and authenticated match APIs. Phase 3A adds transactional notification work,
rolling digests, and reliable one-shot hosted SMTP delivery. PostgreSQL remains
separate from watcher SQLite state. Automated watcher execution, scheduled
imports, and worker scheduling remain unimplemented until Phase 3B.

## Storage design

SQLAlchemy 2.x models live in `app/hosted/models.py`; Alembic revisions live in
`alembic/versions`. UUIDs identify users and opaque records. All timestamps are
timezone-aware UTC. Raw session, verification, and reset tokens are returned
only through a cookie or email link; PostgreSQL stores SHA-256 token hashes.
Passwords use Argon2id.

Preference lists use PostgreSQL JSONB because each bounded list belongs to one
user, is replaced as a unit, and is not queried relationally in Phase 1. API
validation enforces supported enum values, uniqueness, list sizes, and string
lengths before persistence. Company watches use normalized rows with a unique
`(user_id, company_id)` key because they are independently paused and replaced
transactionally.

## Hosted jobs and import tracking

Alembic revision `20260802_0002` adds:

- `hosted_jobs`, with a UUID primary key and unique immutable
  `watcher_job_id`. The watcher-generated ID is the sole hosted job identity;
  the hosted backend does not recalculate identity or deduplicate postings.
- `hosted_job_import_runs`, with one unique SHA-256 `source_fingerprint`,
  nonnegative outcome counters, and checked
  `running`/`succeeded`/`failed` completion state.
- `hosted_job_import_attempts`, linked to its source run by a cascading foreign
  key and unique `(import_run_id, attempt_number)`. This preserves failed-attempt
  history when an operator explicitly retries a source.

Posting text uses PostgreSQL `TEXT`; bounded public provenance uses JSONB.
Only an allowlist of adapter type, direct/backstop type, public requisition ID,
and merged public adapter labels is retained. Watcher `extra` objects, source
URLs, credentials, headers, health data, alumni data, and raw payloads are never
copied into provenance.

`first_seen_at` and `created_at` are set only on first hosted insertion.
`last_seen_at` and `updated_at` advance on every later observation, including an
otherwise unchanged job. `closed_at` records the first observed open-to-closed
transition, remains stable across repeated closed observations, clears on
reopen, and is set again if the reopened posting later closes. An observation
older than the stored `last_seen_at` is rejected transactionally rather than
rewinding lifecycle state.

Company names resolve through the same watcher-derived canonical names and
aliases used by `GET /api/companies`, including the watcher's corporate-suffix
normalization; unsupported or unselectable companies are skipped. Before role
mapping, the hosted mapper reuses the watcher internship/co-op predicate and
records non-internships as `not_internship`. It still maps closed internships
so existing hosted jobs can transition to closed. Watcher role classifications
are converted in one hosted mapper; `invalid_role` therefore means an
internship or co-op could not be safely mapped. Recognized relative Workday
posting labels such as `Posted Yesterday` are retained as an unknown
`posting_date` rather than guessed or treated as malformed. Malformed isolated
jobs are skipped with bounded reason codes, while a malformed final-job
collection fails the import.

Structural final-job failures remain a broad `invalid_final_jobs` CLI error.
Operator failure records append only an allowlisted structural subreason and
never posting data or source identifiers.

## Per-user matching

Alembic revision `20260803_0003` adds `hosted_user_job_matches`, with one
historical row per `(user_id, job_id)`. Import reconciliation creates and
refreshes matches inside the job-import transaction. Preference and watchlist
changes reconcile the affected user's existing jobs but never delete match
history. `matched_at` is the first match, `last_matched_at` is the latest
meaningful match observation, and `no_longer_matches_at` records inactive
history. Saving and dismissing are independent user actions.

Matching is deterministic and requires a watched, unpaused company, open job,
selected role, compatible location/remote preference, and compatible season.
The stored reason list uses a bounded allowlist. It contains no descriptions,
raw preferences, source metadata, scores, resumes, or generated ranking data.

Authenticated endpoints are `GET /api/matches`, `GET /api/matches/{id}`, and
`PATCH /api/matches/{id}`. Every lookup is ownership-scoped; another user's ID
returns the ordinary not-found response.

### Recent openings (`include_recent_openings`)

Alembic revision `20260919_0005` adds `hosted_user_preferences`
`include_recent_openings`, NOT NULL with a `true` server default, so accounts
that predate the migration keep the product default. Signup initializes it
explicitly, and `GET`/`PUT /api/preferences` expose it. A `PUT` that omits the
field is accepted and stores `true`, so clients predating it keep working.

When it is on, a job the platform already collected may be admitted as a *new*
match row for a company the user has just started watching, or after a matching
preference changes, provided its posting is no older than a fixed 90 days.
The window is a product rule, not a setting; it lives in one place as
`match_service.RECENT_OPENING_WINDOW_DAYS`.

Admission for a job with no existing match row:

1. The job must be open and must satisfy every ordinary matching rule —
   watched, unpaused company, selected role, compatible location/remote, and
   compatible season. The catch-up relaxes nothing.
2. `posting_date` is authoritative whenever the source supplied one. A
   `posting_date` on or after the watch's start date is an ordinary post-watch
   opening and is admitted regardless of the setting. Only when `posting_date`
   is absent does `first_seen_at >= watch.created_at` serve as the fallback
   post-watch signal.
3. Otherwise, with the setting off, no new row is created.
4. Otherwise the 90-day catch-up applies, inclusive at the boundary:
   `posting_date >= (now - 90 days).date()`, or for postings with no date,
   `first_seen_at >= now - 90 days`. A known-but-old `posting_date` is never
   overridden by a newer `first_seen_at`, and no date is ever rewritten or
   invented.

The same gate runs in both `reconcile_user` and `reconcile_jobs`, so a
pre-watch posting cannot become a match merely because a later import edits the
hosted job row.

**Admission is not expiry.** The gate is consulted only when no `UserJobMatch`
exists. Existing rows bypass it entirely and keep following the ordinary rules,
so a match admitted on day 90 is not deactivated when the posting turns 91 days
old. Turning the setting off never removes matches that were already admitted;
turning it on reconciles the watched companies so eligible openings appear
immediately.

Catch-up populates Matches only. Reconciliation from a watchlist or preference
change never calls `enqueue_import_notifications`, so a historical match creates
no `hosted_notification_batches` or `hosted_notification_items` row and no
email. `matched_at` stays the time the match was actually made and is never
backdated.

`PUT /api/watchlist` applies a transactional diff rather than deleting and
recreating every row, because `UserCompanyWatch.created_at` is the watch-start
boundary the gate reads. An unchanged entry keeps its row and `created_at`; a
pause or resume updates `paused` and `updated_at` only; a removed company's row
is deleted; and re-adding a removed company starts a new watch with a new
`created_at`.

## Durable notifications and delivery

Alembic revision `20260803_0004` adds:

- `hosted_notification_batches`: user/frequency/due state, a deterministic
  Message-ID, attempt count, processing token and 10-minute lease, submission
  marker, terminal timestamps, and a bounded status/error code. Partial indexes
  serve due pending work and expired leases; user history has a separate index.
- `hosted_notification_items`: one lifetime notification per match row, linked
  to both its batch and source import run. Items are pending, sent, or cancelled.
- `hosted_notification_attempts`: unique positive attempt numbers per batch,
  start/completion timestamps, typed outcome, and bounded error code. Recipient
  addresses, message bodies, credentials, and raw provider errors are never
  stored.

Notification creation is part of the successful import transaction. It accepts
only match rows newly inserted by that import when the user is active, verified,
not globally paused, and has `as_detected`, `three_hour`, or `daily` delivery.
The match must be active and undismissed and its job open. Existing matches,
reactivations, job-only updates, preference reconciliation, watchlist changes,
paused users, and repeated succeeded fingerprints do not create work. If
notification persistence fails, the jobs, matches, notification work, and
reported import success roll back together.

Batch windows are deterministic and UTC-based:

- `as_detected`: one user batch per source import run, due immediately.
- `three_hour`: the first item opens a batch due three hours later; later import
  items join it until it is claimed.
- `daily`: the same rolling behavior with a 24-hour delay.

Only an unclaimed pending batch accepts new rolling items. Phase 3A has no user
timezone or preferred delivery-hour fields.

### Changing alert frequency

Switching between the active delivery frequencies never discards a pending
alert. `hosted_notification_items` is unique on `user_job_match_id` for the
row's whole lifetime, so a cancelled item can never be replaced; pending items
are therefore **re-homed** - the existing row keeps its identity,
`user_job_match_id`, and `source_import_run_id`, and only its `batch_id` moves
to a batch for the user's current frequency. No second item is created, and a
re-homed item keeps `status = 'pending'` with no `cancellation_reason`.

The new window is measured from the moment the change is processed, using the
ordinary batch rules above: `as_detected` is due immediately, `three_hour` and
`daily` join or open a rolling batch due three or twenty-four hours later. An
existing unclaimed pending batch for the target frequency is reused. Because
`(user_id, source_import_run_id)` is unique for `as_detected`, a round trip back
to `as_detected` reclaims the slot it previously retired rather than duplicating
it; that is only ever done for a batch this mechanism itself emptied and that
never reached a mail provider.

Both sides are covered. `PUT /api/preferences` re-homes the user's pending
batches; a batch a worker has already claimed is `processing` rather than
`pending`, so the request leaves it alone and the worker re-homes it inside the
transaction holding its lease. The worker does this before `send_started_at` is
written, so a move can never duplicate a message, and the lease-recovery path
still routes an already-submitted batch to `uncertain`. Once a batch's pending
items have left, that batch alone is cancelled with `frequency_changed`; the
moved items are untouched.

Pausing is unchanged. `paused` is not a delivery frequency, so moving to it
re-homes nothing and the worker still cancels the batch and its items with
`frequency_paused`. Returning to an active frequency re-homes whatever is still
pending, and never revives an item that was already cancelled.

Run one bounded delivery pass from the repository root:

```powershell
$env:HOSTED_DATABASE_URL = "postgresql+psycopg://internship_signal:internship_signal_dev@localhost:55432/internship_signal"
$env:PYTHONPATH = ".;backend"
backend\venv\Scripts\python.exe -m app.hosted.deliver_notifications --limit 25
```

The limit must be 1 through 100. The command recovers expired leases, then
claims due pending rows with `FOR UPDATE SKIP LOCKED`, a random token, and a
10-minute lease. It commits the claim before rendering or network I/O and never
holds a database transaction open during transport. Immediately before transport
it revalidates the account, current frequency, matches, dismissals, and job-open
state; writes `send_started_at` plus the new attempt; and commits again. Token
verification protects every result update.

Job-alert delivery picks its transport with `configured_notification_transport`,
which follows the same precedence as hosted account mail and reads the same
settings:

1. **Resend HTTPS** when `HOSTED_RESEND_API_KEY` and `HOSTED_RESEND_FROM_EMAIL`
   are both set.
2. **SMTP** when Resend is absent and `HOSTED_SMTP_HOST` plus
   `HOSTED_SMTP_FROM_EMAIL` are set.
3. **Unavailable** otherwise: every batch records a bounded permanent
   `mail_not_configured` rather than appearing delivered.

There are no separate job-alert mail variables, so account mail and job-alert
mail cannot drift onto different providers, and a half-configured Resend pair is
still rejected when settings load. Railway Free, Trial, and Hobby plans block
outbound SMTP, so those deployments must configure Resend for job alerts exactly
as they already must for verification mail.

An expired lease without `send_started_at` safely returns to pending. An
expired lease after that marker completes the in-flight attempt as `uncertain`
and never retries automatically because provider acceptance cannot be proven.
Explicit retryable failures wait 1 minute, 5 minutes, 15 minutes, then 1 hour.
The fifth failed attempt becomes `permanent_failed` with `retry_exhausted`.

Both transports classify conservatively and by phase, so the retry and
uncertainty guarantees are identical:

- **SMTP.** Authentication, sender rejection, definitive recipient rejection,
  and SMTP 5xx data rejection are permanent. Safe pre-submission connection
  failures and SMTP 4xx rejection retry. Disconnects, timeouts, and unexpected
  errors after submission may have begun become terminal `uncertain`.
- **Resend.** A 2xx is `sent`. 401/403 is permanent
  (`resend_authentication_failed`); other 4xx is permanent
  (`resend_request_rejected`). 408, 429, and 5xx retry
  (`resend_request_timeout`, `resend_rate_limited`, `resend_server_error`).
  Connect and pool failures, which are known to precede submission, retry as
  `resend_connection_failed_before_submission`. Any other transport failure may
  have reached the wire and becomes `resend_uncertain_after_submission`; an
  unrecognized status becomes `resend_response_unknown`.

Only the exception type and the HTTP status are ever consulted. API keys,
recipient addresses, message bodies, and provider response bodies never reach a
log, an exception message, or a persisted error code.

Every attempt reuses the batch's Message-ID; the Resend transport forwards it as
a custom `Message-ID` header, so it is stable over HTTPS too. Mail resolves the
current verified
account email only at send time and includes plain text plus escaped, simple
HTML. It shows company, role, location, remote status, posting date/deadline,
human-readable allowlisted match reasons, application URL, matches dashboard,
and notification settings. At most 25 jobs are rendered; a remaining count
links to the dashboard, while success marks all valid batch items sent. Mail
contains no description, requirements, raw source metadata, internal IDs,
tracking pixels, or external images. Logs contain only bounded batch IDs,
counts, outcome codes, and timings.

Delivery is deliberately one-shot. It does not run the watcher, import on a
schedule, or loop as a daemon, and this repository still installs no deployment
scheduling: nothing here invokes `app.hosted.deliver_notifications`. Batches
stay pending until an operator, or a scheduler an operator configures by hand,
runs the command. See [Scheduled hosted pipeline](#scheduled-hosted-pipeline).

## Offline snapshot import

From the repository root, after applying migrations:

```powershell
$env:HOSTED_DATABASE_URL = "postgresql+psycopg://internship_signal:internship_signal_dev@localhost:55432/internship_signal"
$env:PYTHONPATH = ".;backend"
backend\venv\Scripts\python.exe -m app.hosted.import_snapshot --snapshot watcher\collection-snapshots\capture.json.gz
```

The command uses the official snapshot loader, checks collection-configuration
compatibility, and runs the snapshot rows through the existing watcher
deduplication and analysis pipeline using the captured UTC date. It does not
collect from the network, open watcher SQLite, send email, mark watcher-seen
jobs, persist health/comparison state, or prime watcher notifications. It does
create Phase 2B matches and eligible Phase 3A notification work transactionally.
There is no HTTP import route. This CLI and the scheduled
`app.hosted.collect_and_import` command share one implementation,
`import_snapshot.import_snapshot_into_hosted`; this one replays a snapshot file
an operator already has, while the scheduled command collects one first.

Collection snapshots do not currently contain a unique content fingerprint;
their existing digest covers collection configuration. Phase 2A therefore uses
SHA-256 of the exact validated compressed snapshot bytes, checked before and
after loading. Reusing a succeeded fingerprint is an idempotent no-op. A
`running` fingerprint is rejected. A failed fingerprint requires the explicit
`--retry-failed` flag; the same run is reused and a new attempt row preserves
the prior failure audit trail. Use `--allow-collection-config-mismatch` only for
an intentional replay under changed collection configuration.

Inspect recent import outcomes without exposing posting text or raw sources:

```sql
SELECT source_identifier, source_type, status, started_at, completed_at,
       jobs_received, jobs_inserted, jobs_updated, jobs_unchanged,
       jobs_skipped, matches_created, failure_summary
FROM hosted_job_import_runs
ORDER BY created_at DESC;
```

## Scheduled hosted pipeline

The hosted product runs as **two independent one-shot commands**. They are
never merged into a daemon and never run inside Uvicorn, so a collection
failure can never stop already-created notification work from being delivered,
and a mail-provider outage can never stop new postings from being collected.

```
A: collect_and_import    sources -> snapshot -> hosted jobs -> matches -> notification work
B: deliver_notifications due batches -> Resend/SMTP -> email
```

### A. Hosted collection and import

```bash
PYTHONPATH=.:backend python -m app.hosted.collect_and_import
```

```powershell
$env:PYTHONPATH = ".;backend"
backend\venv\Scripts\python.exe -m app.hosted.collect_and_import
```

It collects once with the existing watcher source adapters, freezes the result
as a validated collection snapshot in a private temporary directory, and
replays that snapshot through the same `import_snapshot_into_hosted` path the
operator CLI uses. There is one hosted import path, not two, so job identity,
deduplication, snapshot validation, matching, and notification enqueueing
cannot drift between them.

`--watchlist` overrides the watcher configuration; there are no other options.
Runs are recorded in `hosted_job_import_runs` with
`source_type = 'hosted_collection'`, which distinguishes scheduled collection
from an operator's manual `collection_snapshot` replay.

**It does not touch legacy watcher state.** Collection in `watcher.collection`
is network and parsing only: no seen store is opened, no source-health or
analysis-cache database is written, and the digest sender is never reached.
Running this command therefore cannot suppress, duplicate, or advance the
legacy personal digest, and `.github/workflows/watcher.yml` is unaffected.

**Temporary artifacts.** The snapshot lives in a `tempfile.mkdtemp()` workspace
outside the repository and is removed on success, on failure, and on an
unexpected error alike. No runtime snapshot is ever written to a tracked path.

**Exit codes.** `0` success, `1` collection/validation/import failure, `2`
`HOSTED_DATABASE_URL` (or `DATABASE_URL`) not configured. Logs are counts and a
truncated fingerprint only - never posting text, company names, source URLs, or
raw source errors. A failure never prints an import summary, because the
`HOSTED-JOB-IMPORT` line is emitted only after the import transaction commits.

**Idempotency.** The snapshot is written deterministically, so re-running the
identical collection produces the same SHA-256 source fingerprint and the
import is a recognised `already_imported` no-op. A later collection of the same
posting is a new fingerprint but the same `watcher_job_id`, so the job is
upserted, no duplicate match is created, and no alert repeats.

### B. Notification delivery

```bash
PYTHONPATH=.:backend python -m app.hosted.deliver_notifications --limit 25
```

Unchanged by scheduling: it is still the bounded, leased, one-shot worker
described above, and it still selects Resend HTTPS before SMTP through
`configured_notification_transport`.

### Recommended Railway schedules

**Scheduling is not enabled by this repository.** No cron, no worker service,
and no GitHub Actions schedule is created here; the workflow in
`.github/workflows/watcher.yml` remains the legacy watcher only and never
contacts the hosted database. Everything below is a plan an operator applies by
hand in the Railway dashboard.

A Railway cron service runs its start command on a schedule and exits, which
is what both commands already do. Schedules use standard five-field cron
expressions in UTC.

Confirm the minimum cron interval allowed on the current Railway plan before
applying the five-minute schedule below; if a shorter interval than the plan
permits is rejected, use `*/15 * * * *` instead. The only cost is latency:
`as_detected` alerts would be delivered up to fifteen minutes after the import
that created them, and the rolling three-hour and daily windows are unaffected.

Overlapping runs are safe either way, so neither schedule depends on the
platform skipping a run. The delivery worker claims batches with
`FOR UPDATE SKIP LOCKED` under a token and a 10-minute lease, and collection
claims each source fingerprint exactly once and rejects a `running` one, so two
concurrent passes cannot double-send or double-import.

| Service | Command | Schedule | Why |
| --- | --- | --- | --- |
| `hosted-collection` | `python -m app.hosted.collect_and_import` | `17 * * * *` | Hourly matches the legacy watcher cadence and the rate at which sources change. The off-the-hour minute keeps it clear of the GitHub Actions watcher run. |
| `hosted-notifications` | `python -m app.hosted.deliver_notifications --limit 25` | `*/5 * * * *` | Five minutes keeps `as_detected` alerts prompt while staying far inside the 10-minute lease and the retry backoff. |

Both need `PYTHONPATH=.:backend` and the same `HOSTED_DATABASE_URL`/
`DATABASE_URL` the API uses. `hosted-notifications` additionally needs the mail
provider variables (`HOSTED_RESEND_API_KEY`, `HOSTED_RESEND_FROM_EMAIL`) and
`HOSTED_PUBLIC_FRONTEND_URL` for the links in the digest. `hosted-collection`
needs no mail variables at all, because an import creates notification work but
never delivers it.

**Railway requires these as two separate services**, each with its own start
command and cron schedule, alongside the existing API service. They can share
the repository and the PostgreSQL plugin; they must not share a schedule, and
neither belongs in the API service's start command.

### First run after scheduling is enabled

Production may already hold pending notification batches created before any
worker existed. Enabling a five-minute schedule against that backlog would
deliver all of it at once. Inspect first, with read-only SQL:

```sql
-- What state is the backlog in?
SELECT status, frequency, count(*) AS batches
FROM hosted_notification_batches
GROUP BY status, frequency
ORDER BY status, frequency;

-- How old is the pending work, and is any of it already due?
SELECT count(*) AS pending_batches,
       min(due_at) AS oldest_due_at,
       max(due_at) AS newest_due_at,
       count(*) FILTER (WHERE due_at <= now()) AS already_due
FROM hosted_notification_batches
WHERE status = 'pending';

-- How many alerts would actually be sent?
SELECT i.status, count(*) AS items
FROM hosted_notification_items AS i
JOIN hosted_notification_batches AS b ON b.id = i.batch_id
WHERE b.status = 'pending'
GROUP BY i.status;

-- Is collection actually reaching the database?
SELECT source_type, status, count(*) AS runs,
       max(completed_at) AS most_recent
FROM hosted_job_import_runs
GROUP BY source_type, status
ORDER BY most_recent DESC NULLS LAST;
```

Then run the worker once by hand, deliberately small, and inspect again before
adding the cron schedule:

```bash
PYTHONPATH=.:backend python -m app.hosted.deliver_notifications --limit 5
```

Check the resulting `hosted_notification_attempts` rows and the batches' new
`status`/`last_error_code` before raising the limit or enabling `*/5 * * * *`.

Nothing here resends history. `permanent_failed` and `cancelled` batches are
terminal: the worker only claims `pending` batches whose `due_at` and
`next_attempt_at` have passed, and no command in this repository moves a
terminal batch back to `pending`. A batch that failed while no mail provider
was configured recorded `mail_not_configured` as a permanent failure on its
first attempt and will **not** retry once Resend is configured - configure the
provider before scheduling the worker, and treat any pre-existing
`permanent_failed` rows as a separate, deliberate decision.

## Local startup

From the repository root:

```powershell
docker compose -f docker-compose.hosted.yml up -d postgres
$env:HOSTED_DATABASE_URL = "postgresql+psycopg://internship_signal:internship_signal_dev@localhost:55432/internship_signal"
backend\venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
```

Create the virtual environment first when needed:

```powershell
py -m venv backend\venv
backend\venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

Start FastAPI:

```powershell
$env:PYTHONPATH = ".;backend"
backend\venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Run migrations and FastAPI from the same shell so the process environment
contains `HOSTED_DATABASE_URL`. `.env.example` is a reference template; Alembic
does not implicitly read it.

Start the hosted frontend in another shell:

```powershell
Set-Location frontend
$env:VITE_HOSTED_API_MODE = "live"
$env:VITE_HOSTED_API_BASE_URL = "http://localhost:8000"
npm install
npm run dev
```

The committed Compose password is for local development only. Use separately
managed credentials and `HOSTED_SECURE_COOKIES=true` in production.

## Migrations and tests

Alembic requires a hosted PostgreSQL URL and never uses watcher SQLite. It
reads `HOSTED_DATABASE_URL`, then `DATABASE_URL`, through the same resolver the
FastAPI runtime uses, so the two cannot disagree:

```powershell
backend\venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
```

PostgreSQL integration tests require an administrative test connection. The
fixture creates a random database, migrates it from empty, truncates it between
tests, terminates its own connections, and drops it afterward:

```powershell
$env:HOSTED_TEST_DATABASE_URL = "postgresql+psycopg://internship_signal:internship_signal_dev@localhost:55432/postgres"
Set-Location backend
venv\Scripts\python.exe -m pytest tests -q

Set-Location ..\frontend
npm test -- --run
npm run build
```

Never point `HOSTED_TEST_DATABASE_URL` at staging, production, watcher state, or
a database containing data that must be retained.

## Environment variables

- `HOSTED_DATABASE_URL` (required for hosted persistence and Alembic; when it
  is unset or blank, the platform-standard `DATABASE_URL` is used instead, which
  is what Railway and most managed PostgreSQL add-ons export). `postgres://`
  and `postgresql://` URLs are normalized to `postgresql+psycopg://`.
- `HOSTED_SESSION_LIFETIME_SECONDS`
- `HOSTED_SESSION_COOKIE_NAME`
- `HOSTED_SECURE_COOKIES` (also selects the session cookie's SameSite
  policy: `true` issues `Secure; SameSite=None` so a separately hosted HTTPS
  frontend can send it on cross-site credentialed requests, `false` keeps
  `SameSite=Lax` for local HTTP development)
- `HOSTED_ALLOWED_FRONTEND_ORIGINS` (explicit comma-separated origins; no `*`)
- `HOSTED_VERIFICATION_TOKEN_LIFETIME_SECONDS`
- `HOSTED_PASSWORD_RESET_TOKEN_LIFETIME_SECONDS`
- `HOSTED_PUBLIC_FRONTEND_URL`
- `HOSTED_RESEND_API_KEY`, `HOSTED_RESEND_FROM_EMAIL` (sending address only,
  such as `noreply@example.com`; set both or neither)
- `HOSTED_SMTP_HOST`, `HOSTED_SMTP_PORT`, `HOSTED_SMTP_USERNAME`
- `HOSTED_SMTP_PASSWORD`, `HOSTED_SMTP_FROM_EMAIL`, `HOSTED_SMTP_STARTTLS`
- `HOSTED_SMTP_TIMEOUT_SECONDS` (also bounds the Resend HTTPS request)
- `VITE_HOSTED_API_MODE=live`
- `VITE_HOSTED_API_BASE_URL` (optional with a same-origin proxy)

`app.hosted.collect_and_import` needs only the database URL.
`app.hosted.deliver_notifications` needs the database URL, the mail provider
variables, and `HOSTED_PUBLIC_FRONTEND_URL`. Both need `PYTHONPATH=.:backend`.

### Mail provider selection

Account mail (verification and password reset) and hosted job-alert mail read
the same `HOSTED_RESEND_*` and `HOSTED_SMTP_*` settings and pick exactly one
provider each, in this order:

1. **Resend HTTPS** when `HOSTED_RESEND_API_KEY` and `HOSTED_RESEND_FROM_EMAIL`
   are both set. Messages are posted to the Resend REST API over HTTPS.
2. **SMTP** when Resend is absent and `HOSTED_SMTP_HOST` plus
   `HOSTED_SMTP_FROM_EMAIL` are set.
3. **Disabled** otherwise: the API reports that delivery did not happen rather
   than pretending it did.

Setting only one of the two Resend values raises at startup, so a deployment
never silently falls back to a provider the operator did not choose. The API
key is excluded from the settings `repr` and never appears in logs, exception
messages, or responses; provider response bodies are likewise never logged or
returned.

Railway Free, Trial, and Hobby plans block outbound SMTP, so deployments on
those plans must configure the Resend HTTPS provider. This covers job alerts as
well as account mail: `mailer.configured_mailer` and
`notification_mail.configured_notification_transport` apply the same precedence
to the same settings. Verification and
password-reset links are always built from `HOSTED_PUBLIC_FRONTEND_URL`, so
that variable must point at the public site, for example
`https://app.example.com/verify-email?token=<opaque token>`.

When no provider is configured, or a provider rejects a message, the API does
not claim delivery.
Forgot-password and resend-verification responses remain identical for known
and unknown accounts. Password-reset mail is delivered after the generic
forgot-password response, and a successful reset invalidates every outstanding
reset token plus every active session for that user.

Only `HOSTED_DATABASE_URL` is required by the snapshot-import command. Imports
create durable notification work but do not deliver it, so no mail provider is
required for an import.
Watcher email and watcher SQLite settings are neither required nor used.
