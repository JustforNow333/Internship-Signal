# Hosted backend Phases 1 and 2A

Phase 1 adds persistent multi-user accounts, preferences, company watchlists,
verification/reset tokens, and unsupported-company requests. PostgreSQL is the
authoritative datastore. Phase 2A adds central final-job persistence and
idempotent offline snapshot imports. PostgreSQL remains separate from watcher
SQLite state. User-job matching, `/api/matches`, synchronization scheduling,
and internship alert delivery are still unimplemented.

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
  nonnegative outcome counters, `matches_created = 0`, and checked
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
collect from the network, open watcher SQLite, send email, create matches, mark
seen jobs, persist health/comparison state, or prime notifications.
Imports are operator-only CLI operations; Phase 2A adds no HTTP import route.

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
Set-Location backend
venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
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

Alembic requires `HOSTED_DATABASE_URL` and never uses watcher SQLite:

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

- `HOSTED_DATABASE_URL` (required for hosted persistence and Alembic)
- `HOSTED_SESSION_LIFETIME_SECONDS`
- `HOSTED_SESSION_COOKIE_NAME`
- `HOSTED_SECURE_COOKIES`
- `HOSTED_ALLOWED_FRONTEND_ORIGINS` (explicit comma-separated origins; no `*`)
- `HOSTED_VERIFICATION_TOKEN_LIFETIME_SECONDS`
- `HOSTED_PASSWORD_RESET_TOKEN_LIFETIME_SECONDS`
- `HOSTED_PUBLIC_FRONTEND_URL`
- `HOSTED_SMTP_HOST`, `HOSTED_SMTP_PORT`, `HOSTED_SMTP_USERNAME`
- `HOSTED_SMTP_PASSWORD`, `HOSTED_SMTP_FROM_EMAIL`, `HOSTED_SMTP_STARTTLS`
- `HOSTED_SMTP_TIMEOUT_SECONDS`
- `VITE_HOSTED_API_MODE=live`
- `VITE_HOSTED_API_BASE_URL` (optional with a same-origin proxy)

When SMTP is absent or rejects a message, the API does not claim delivery.
Forgot-password and resend-verification responses remain identical for known
and unknown accounts. Password-reset mail is delivered after the generic
forgot-password response, and a successful reset invalidates every outstanding
reset token plus every active session for that user.

Only `HOSTED_DATABASE_URL` is required by the snapshot-import command. SMTP,
session-cookie, watcher email, and watcher SQLite settings are neither required
nor used by imports.
