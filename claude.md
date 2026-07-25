# Claude Repository Guide

- After every user prompt, update the root `claude.md`, `agents.md`, and
  `.gitignore`. Keep them concise, synchronized, and relevant.
- Read `agents.md` before repository work. Before watcher work, also read
  `WATCHER_SPEC.md` in full; use `WATCHER_PROGRESS.md` only for current status.
- Change only the requested layer and preserve unrelated work.
- Keep CSV cleanup in `process_csv`; use `analyze_rows` for canonical rows.
  Never duplicate backend scoring, classification, signals, dedupe, or IDs.
- Source adapters only fetch canonical rows. Eligibility belongs in
  `watcher/eligibility.py`; filters add internship/open/min-score checks.
- Keep typed GitHub sources backward-compatible with `github_listing_urls`.
  Merge direct ATS, Simplify JSON, then Markdown by fixed priority; Markdown
  `Added` is source metadata and lower-priority closure cannot close direct data.
- Row provenance keys off `extra.source_adapter`, which `make_row` always sets.
  CSV `extra` is user data and never drives dedupe ordering or provenance.
- Track each GitHub source independently; valid feeds with zero matches succeed.
- Mark jobs seen only after a live send or explicit priming. Alumni data is
  additive and private.
- Never commit `.env`, credentials, alumni data, SQLite state, probe/health
  output, or `evaluation/private/`.
- Workday: log safe metadata only; retry only transient failures; never treat
  HTML as empty or use anti-bot evasion; never reset persistent state.
- Sanitizers are total: `sanitize_error`, `sanitize_feed_label`, and `_safe_url`
  run over arbitrary failure text and must never raise on a malformed URL.
- Tests and benchmark evaluation stay offline. Benchmarking must not alter
  scoring and must not use alumni, email, seen state, or workflow persistence.
- Benchmark labels require `human_eligible` (`yes`, `no`, or `uncertain`);
  optional role track, exclusion reason, and notes do not affect binary metrics.
- Use the validation commands in `agents.md`; always run `git diff --check`.
