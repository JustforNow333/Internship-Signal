# Hosted UI API contract

The hosted MVP uses `src/hosted/api.js` as its only data boundary. It defaults
to an in-memory mock because this repository does not yet implement multi-user
authentication, preferences, watchlists, matches, or company-request storage.
Set `VITE_HOSTED_API_MODE=live` to use the HTTP adapter.

The adapter expects JSON from:

| Method | Endpoint | Request body | Response |
| --- | --- | --- | --- |
| GET | `/api/companies` | — | supported company objects |
| GET | `/api/me` | — | current user plus `last_successful_scan_at` |
| POST | `/api/auth/signup` | `email`, `password` | user/session result |
| POST | `/api/auth/login` | `email`, `password` | user/session result |
| POST | `/api/auth/forgot-password` | `email` | accepted result |
| GET | `/api/preferences` | — | role, location, season, and alert preferences |
| PUT | `/api/preferences` | complete or partial preferences | saved preferences |
| GET | `/api/watchlist` | — | `{company_id, paused}` entries |
| PUT | `/api/watchlist` | `{companies: [...]}` | saved entries |
| GET | `/api/matches` | — | newest-first match objects |
| POST | `/api/company-requests` | `company_name`, optional `career_url` | request receipt |

Matches include `id`, `company_id`, `company`, `title`, `role_id`, `role`,
`location`, `remote`, ISO `detected_at`, `why`, posting text fields, and a safe
employer `source_url`. Authentication is expected to use a secure HTTP-only
session cookie; the frontend does not store tokens.
