# Hosted Phase 1 API contract

`src/hosted/api.js` is the hosted UI's only data boundary. Mock mode remains
the default for isolated UI tests. Set `VITE_HOSTED_API_MODE=live` to use the
FastAPI endpoints and optionally set `VITE_HOSTED_API_BASE_URL` when the API is
not served through Vite's `/api` proxy.

All live requests use `credentials: "include"`. Authentication is a
server-managed PostgreSQL session delivered in an HTTP-only cookie; the
frontend never stores a token in local or session storage. An HTTP 401 emits a
hosted unauthorized event, and protected routes return to `/signin`.

| Method | Endpoint                        | Authentication | Request                               | Response                               |
| ------ | ------------------------------- | -------------- | ------------------------------------- | -------------------------------------- |
| POST   | `/api/auth/signup`              | Public         | `email`, `password`                   | `user`, `verification_email_sent`      |
| POST   | `/api/auth/login`               | Public         | `email`, `password`                   | `user` and session cookie              |
| POST   | `/api/auth/logout`              | Required       | —                                     | `204`, revoked session, cleared cookie |
| POST   | `/api/auth/forgot-password`     | Public         | `email`                               | generic `{accepted: true}`             |
| POST   | `/api/auth/reset-password`      | Public         | `token`, `password`                   | `{accepted: true}`                     |
| POST   | `/api/auth/resend-verification` | Public         | `email`                               | generic `{accepted: true}`             |
| POST   | `/api/auth/verify-email`        | Public         | `token`                               | `{accepted: true}`                     |
| GET    | `/api/me`                       | Required       | —                                     | safe current-user fields only          |
| GET    | `/api/companies`                | Public         | —                                     | sanitized watcher-derived catalog      |
| GET    | `/api/preferences`              | Required       | —                                     | current user's preferences             |
| PUT    | `/api/preferences`              | Required       | complete preference object            | saved preferences                      |
| GET    | `/api/watchlist`                | Required       | —                                     | current user's watch entries           |
| PUT    | `/api/watchlist`                | Required       | `{companies: [{company_id, paused}]}` | complete replacement                   |
| POST   | `/api/company-requests`         | Required       | `company_name`, optional `career_url` | request receipt                        |

Preference role IDs are `software_engineering`, `machine_learning_ai`,
`data_science`, `data_engineering`, `quantitative_development`,
`product_management`, `hardware_embedded`, and `other_engineering`. Alert
frequencies are `as_detected`, `three_hour`, `daily`, and `paused`.

Company objects expose only `id`, `name`, optional `aliases`, `coverage`, and
`selectable`. Coverage is `direct`, `backstop`, or (only with reliable current
evidence) `delayed`. ATS tokens, Workday configuration, source URLs, health
details, notes, and alumni data are never part of this response.

Phase 1 intentionally has no job matching endpoint. In live mode the adapter
returns an empty matches collection so account, preference, and watchlist pages
remain usable. Mock mode retains match fixtures for UI regression tests.
There is also no account-deletion endpoint in this phase, so the Settings
control is visibly disabled instead of pretending that navigation deleted data.

Validation failures return HTTP 422 with safe entries containing `field`,
`message`, and `type`; raw request values are omitted. HTTP 401 means the
session is missing, invalid, expired, revoked, or belongs to an inactive user.
