# Hosted backend Phase 1

Phase 1 adds persistent multi-user accounts, preferences, company watchlists,
verification/reset tokens, and unsupported-company requests. PostgreSQL is the
authoritative datastore. It is separate from watcher SQLite state and does not
collect jobs, match postings, or deliver internship alerts.

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
