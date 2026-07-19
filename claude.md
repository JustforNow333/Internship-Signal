# Claude repository guidance

Read `agents.md` and `WATCHER_SPEC.md` in full before watcher work. They are the
authoritative implementation and validation guides; this file records only the
Workday transport constraints most likely to be missed during incident work.

- Treat simultaneous Workday decode failures as a shared transport path first,
  while preserving one independent health attempt per company.
- Never log or persist raw response bodies, cookies, sensitive headers, tokens,
  query strings, or complete challenge pages. Use the structured safe metadata
  and stable `SourceFetchError.error_code` classifications from
  `watcher/sources/base.py`.
- Retry only transient Workday transport failures. Keep three total attempts,
  bounded injectable backoff, capped `Retry-After`, and instance-local pacing.
  Do not retry configuration errors or valid-JSON schema failures.
- `WATCHER_WORKDAY_MIN_INTERVAL_SECONDS` defaults to `0.5`, accepts `0` through
  `10`, and `0` disables cross-tenant pacing for a controlled diagnostic.
- HTML is never an empty board. Do not undo posting-level malformed-record
  isolation or all-malformed-feed failure behavior.
- Do not reset `watcher-data`, source-health counters, seen rows, or recovery
  history. Do not add cookies, proxy rotation, CAPTCHA/challenge bypass,
  browser automation, or other anti-bot evasion.
- Safe probes use `scripts/probe_workday_transport.py`, explicitly set
  `WATCHER_SEND_EMAIL=0`, never pass `--mark-seen-without-send`, and never use a
  production seen database. The manual Actions probe is isolated from
  `watcher-data`, alumni data, and SMTP.
- Keep automated tests offline. Use injected request functions, clocks,
  sleepers, and jitter, then run the full backend/watcher suite, compile check,
  workflow YAML validation, and `git diff --check`.
