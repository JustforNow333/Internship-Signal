# Claude Repository Guide

- After every user prompt, update the root `claude.md`, `agents.md`, and
  `.gitignore`. Keep them concise, synchronized, and relevant.
- Read `agents.md` before repository work. Before watcher work, also read
  `WATCHER_SPEC.md` in full; use `WATCHER_PROGRESS.md` only for current status.
- Change only the requested layer and preserve unrelated work.
- Route JSON endpoints through `_json_object`; malformed or excessively nested
  JSON and invalid request shapes are HTTP 400, never internal errors.
- Keep CSV cleanup in `process_csv`; use `analyze_rows` for canonical rows.
  Never duplicate backend scoring, classification, signals, dedupe, or IDs.
- Source adapters only fetch canonical rows. Eligibility belongs in
  `watcher/eligibility.py`; filters add internship/open/min-score checks.
- Student-status exclusions require clear mandatory evidence and use stable
  `phd_only`, `graduate_only`, `freshman_only`, or
  `returning_intern_only` reasons. Mixed, preferred, incidental, or ambiguous
  mentions remain eligible. Evaluate them only after internship/student-program
  and open checks; traces retain mandatory, negation, and mixed diagnostics.
- `Master data`, `master record`, `master dataset`, and `master schedule` are
  operational terms, not graduate-degree evidence; bare `master` needs explicit
  degree, program, student, or candidate context.
- `assess_us_location` is the sole location gate: explicit U.S. wins,
  explicit foreign country/region yields `outside_us`, and ambiguity passes.
  Prefer collected structured country/location metadata, keep city-only
  evidence ambiguous, preserve diagnostics, and never derive country from
  state abbreviations or alter scores/ranking.
- Role rules prioritize title/core duties over incidental keywords. Technical
  AI integration, digital solutions, quant, product, and umbrella programs
  require explicit central technical evidence; physical engineering, consumer
  research, and manufacturing quality remain excluded.
- Keep typed GitHub sources backward-compatible with `github_listing_urls`.
  Merge direct ATS, Simplify JSON, then Markdown by fixed priority; Markdown
  `Added` is source metadata and lower-priority closure cannot close direct data.
- Row provenance keys off `extra.source_adapter`, which `make_row` always sets.
  CSV `extra` is user data and never drives dedupe ordering or provenance.
- Track each GitHub source independently; valid feeds with zero matches succeed.
- Dry runs never change notification state. Live sends populate `emailed_at`
  only after success; explicit priming has its own marker, and unmarked legacy
  rows remain pending.
- Watcher audits are read-only and reuse production identity, dedupe,
  classification, eligibility, scoring, and seen lookup; state-only audits
  never fetch, and live audits never email, prime, or persist health attempts.
- Source comparisons retain 30 aggregate runs, three detail runs, all bounded
  eligible/anomaly detail, and deterministic routine-rejection samples per
  reason; compact only after material cleanup. Health-alert
  cooldowns use dedicated tables and an independent email switch/renderer;
  they never update internship `emailed_at` or `primed_at`.
- Rollout verification disables internship email, priming, Workday probes, and
  health email; preserve notification timestamps and existing repository
  variables before enabling conservative health alerts.
- Collection and notification share one identity policy: stable requisition
  ID, posting-specific normalized URL, then exact company/title/location.
  Generic URLs never collapse distinct stable requisitions.
- Alumni data is additive and private.
- Never commit `.env`, credentials, alumni data, SQLite state, probe/health
  output, downloaded or extracted Actions diagnostics, or
  `evaluation/private/`.
- Workday: log safe metadata only; retry only transient failures; never treat
  HTML as empty or use anti-bot evasion; never reset persistent state.
- Sanitizers are total: `sanitize_error`, `sanitize_feed_label`, and `_safe_url`
  run over arbitrary failure text and must never raise on a malformed URL.
- Tests and benchmark evaluation stay offline. Benchmarking must not alter
  scoring and must not use alumni, email, seen state, or workflow persistence.
- Frozen benchmarks retain international rows; current evaluation applies the
  production eligibility helper without regenerating frozen inputs.
- Never rewrite `scoring_20260724_*`. U.S. role-fit benchmarking uses the
  separate exporter and only `us`/`ambiguous` candidates with independent
  `random`, `likely_match`, and `difficult_negative` cohorts.
- Rebuild U.S. role-fit artifacts only from a clean commit, validate manifest
  hashes and `git_dirty=false`, and explain every change from the prior export.
- Benchmark labels require `human_eligible` (`yes`, `no`, or `uncertain`);
  optional role track, exclusion reason, and notes do not affect binary metrics.
- Use the validation commands in `agents.md`; always run `git diff --check`.
- Bug audits require a reproducible failure or clear violated invariant. Add a
  regression test before fixing behavior; do not change code for style alone.
- Repository-wide cleanup must preserve public shapes and side effects; remove
  duplication only after tests cover every consolidated caller.
- Holdout construction is two-stage: commit reusable tooling first, then
  collect from that exact clean SHA. Exclude both prior benchmarks by stable
  ID, normalized URL, and fallback key without reading their human labels.
