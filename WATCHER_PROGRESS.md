# Internship Watcher - progress & handoff

## Source Of Truth

`WATCHER_SPEC.md` in the repo root remains the authoritative build spec.
This file tracks completed watcher steps and the next handoff target.

## Current Status

- Backend `analyze_rows(rows, today=None)` seam is built and reused by watcher.
- Watcher source layer is built: Greenhouse, Lever, Ashby, SmartRecruiters,
  Workable, Workday, and SimplifyJobs GitHub listings.
- `watcher/detect.py` and the generated priority `watcher/watchlist.yml` are in
  place.
- `watcher/alumni.py` is built and real-data verified against the private
  gitignored `watcher/alumni.csv` roster. Alumni annotations are additive only.
- `watcher/notify.py` is built for the email digest. Dry-run is the default;
  live Gmail SMTP is opt-in via env.

## Done

1. `analyze_rows` refactor.
2. Source adapters, watcher core, detect helper, generated watchlist, and
   alumni join.
   - Real roster verification: `watcher/alumni.csv` is present with 332 data
     rows. It has the five required columns plus an ignorable extra
     `First and last name` column.
   - `load_alumni()` indexes 306 records across 278 employer keys. The 26-row
     gap is entirely blank `Employer` values; the loader drops no duplicate
     rows and found no other unindexable nonblank employers.
   - No rows have blank first/last-name fields while the combined
     `First and last name` field is populated.
   - Real watchlist fuzzy matches found no obvious false positives. The current
     logged fuzzy matches are Balyasny Asset Management, Chainalysis, MIT
     Lincoln Laboratory, and Northrop Grumman against roster spelling variants.
   - The roster typo `Capitol One` now attaches to `Capital One` postings via
     the alias tier. `Chainalysis` continues to attach to the roster typo
     `Chainanalysis` through the fuzzy tier.
3. Email digest:
   - `render_digest(matches)` is pure and offline-tested.
   - `send_digest(matches)` sends nothing for zero new matches.
   - Dry-run prints the rendered digest unless `WATCHER_SEND_EMAIL` is truthy.
   - Live send requires `SMTP_USER`, `SMTP_APP_PASSWORD`, and `EMAIL_TO`.
   - The seen-store advances only after a successful live send; dry-run digest
     previews do not mark postings seen.
   - Digest includes all new SWE-intern matches with no score gate, sorted by
     score descending.
   - Digest rows show score, recommendation, top reason, red flags, apply URL,
     source tag, and alumni annotations.
   - Current suite: `141 passed, 1 warning`.

## Next

4. Scheduler:
   - Add GitHub Actions hourly cron plus manual dispatch.
   - Preserve the SQLite seen-store across ephemeral runners.
   - Use repository secrets for SMTP env vars.
   - Surface partial failures loudly so scraper breakage is visible.

## Validation Command

```bash
PYTHONPATH=.:backend backend/venv/Scripts/python.exe -m pytest backend/tests watcher/tests -q
```
