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
| DELETE | `/api/me`                       | Required + CSRF | `password`                           | `204`, account deleted, cleared cookie |
| GET    | `/api/companies`                | Public         | —                                     | sanitized watcher-derived catalog      |
| GET    | `/api/preferences`              | Required       | —                                     | current user's preferences             |
| PUT    | `/api/preferences`              | Required       | complete preference object            | saved preferences                      |
| GET    | `/api/watchlist`                | Required       | —                                     | current user's watch entries           |
| PUT    | `/api/watchlist`                | Required       | `{companies: [{company_id, paused}]}` | complete replacement                   |
| POST   | `/api/company-requests`         | Required       | `company_name`, optional `career_url` | request receipt                        |

Preference role IDs are `software_engineering`, `machine_learning_ai`,
`data_science`, `data_engineering`, `quantitative_development`,
`quantitative_trading_research`, `product_management`, `hardware_embedded`,
and `other_engineering`. Alert
frequencies are `as_detected`, `three_hour`, `daily`, and `paused`.

Company objects expose only `id`, `name`, optional `aliases`, `coverage`, and
`selectable`. Coverage is `direct`, `backstop`, or (only with reliable current
evidence) `delayed`. ATS tokens, Workday configuration, source URLs, health
details, notes, and alumni data are never part of this response.

Match endpoints (`GET /api/matches`, `GET /api/matches/{id}`,
`PATCH /api/matches/{id}`) require authentication and are ownership-scoped:
another user's match is HTTP 404. Match objects include nullable `saved_at`,
`dismissed_at`, and `applied_at`. `GET /api/matches` accepts `view` =
`active` (default), `saved`, `dismissed`, `historical`, `applied`, or `all`
(otherwise HTTP 400), with `limit`/`offset` paging, `matched_at DESC` order, and
an `{items, limit, offset, total, has_more}` envelope; `getMatchView(view)`
loads every page. `view=applied` returns every applied match, including
dismissed, closed, and no-longer-matching ones.

`PATCH` accepts strict-boolean `saved`, `dismissed`, and `applied`, with at
least one present. `applied=true` stamps server time once and keeps the
original date on repeat; `applied=false` clears it. Undo after unmarking sends
`applied=true` with the original `applied_at` string, which must be a
timezone-aware ISO 8601 datetime no later than server now. It cannot be `null`
and is accepted only with `applied=true`. Invalid payloads are HTTP 422 and
change nothing. Saved, Dismissed, and Applied are independent, and Applied
never affects notifications or matching. Mock mode implements the same views,
validation, and Applied semantics on demo fixtures.

`DELETE /api/me` permanently deletes the signed-in account. The adapter's
`deleteAccount({password})` sends the current password and the
`X-FindSooner-CSRF: 1` header; the browser adds `Origin`, which must be one of
`HOSTED_ALLOWED_FRONTEND_ORIGINS`. A wrong password or failed CSRF check is
HTTP 403 (never 401, so it does not trigger the unauthorized redirect) and
changes nothing; a storage failure is HTTP 503. Only after `204` does the app
clear its account state and show a neutral confirmation on `/signin`; it sends
no logout request, because every session was deleted with the account. Mock
mode simulates the same outcome: after a deletion its account calls return 401.

Validation failures return HTTP 422 with safe entries containing `field`,
`message`, and `type`; raw request values are omitted. HTTP 401 means the
session is missing, invalid, expired, revoked, or belongs to an inactive user.
