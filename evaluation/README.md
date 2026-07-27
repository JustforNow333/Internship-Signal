# Real-posting scoring benchmark

This benchmark measures the existing watcher's eligibility decisions and
fit-score ranking against later human labels. It complements the synthetic
pytest regression suite: unit tests protect known rules with constructed edge
cases, while this benchmark freezes a deterministic sample of real collected
postings and asks whether the resulting decisions match the user's actual
internship preferences.

The benchmark is measurement infrastructure. It does not change scoring,
send email, open a seen-store, mark jobs seen, load alumni data, or touch the
`watcher-data` branch.

## 1. Export a private benchmark

From PowerShell at the repository root:

```powershell
$env:WATCHER_SEND_EMAIL = "0"
$env:PYTHONPATH = ".;backend"

backend\venv\Scripts\python.exe scripts\build_scoring_benchmark.py `
  --watchlist watcher/watchlist.yml `
  --as-of 2026-07-20 `
  --seed 20260720 `
  --output-prefix evaluation/private/scoring_20260720
```

POSIX equivalent:

```bash
WATCHER_SEND_EMAIL=0 PYTHONPATH=.:backend python3 scripts/build_scoring_benchmark.py \
  --watchlist watcher/watchlist.yml \
  --as-of 2026-07-20 \
  --seed 20260720 \
  --output-prefix evaluation/private/scoring_20260720
```

The exporter calls `watcher.run.collect_rows()`, then
`backend.app.ingest.analyze_rows()`. The candidate population is every analyzed
job for which both `watcher.filters.is_internship()` and
`watcher.filters.is_open()` return true. It deliberately does not call
`filter_matches()`: currently ineligible and zero-fit internships must remain
available for human review.

Source failures are nonfatal when usable postings remain and are recorded in
the manifest. A run with no collected rows or no open internship candidates
fails without writing a benchmark set.

### Sampling design

The default cohorts are independently selected and then emitted as one stable,
deduplicated sequence:

- `random`: up to 100 postings sampled from the entire candidate population
  with `random.Random(seed)`. Only this cohort supports population-style
  eligibility precision and recall.
- `top`: up to 30 postings sorted by fit score descending, generic total score
  descending, company, title, and stable job ID.
- `difficult`: up to 50 postings selected by deterministic seeded round-robin
  across documented difficult role tracks and the 1–24, 25–44, 45–69, 70–84,
  and 85–100 fit-score bands.

Cohorts are selected independently so the random cohort remains statistically
interpretable. A posting selected by multiple cohorts appears once and lists
all memberships in `sample_groups`; therefore the final unique count may be
below 180. Small candidate pools and sparse strata simply produce smaller
actual counts, recorded in the manifest.

For identical collected canonical rows, as-of date, seed, cohort sizes, and
code version, selected IDs and their order are deterministic.

### Exported files

The prefix above creates:

- `scoring_20260720_labels.csv`: blind human-labeling sheet.
- `scoring_20260720_rows.jsonl`: whitelisted canonical pre-analysis rows used
  for future rescoring.
- `scoring_20260720_predictions.json`: frozen baseline predictions keyed by
  stable job ID.
- `scoring_20260720_manifest.json`: dataset definition, counts, source errors,
  Git state, output paths, and SHA-256 hashes.

Files are prepared as UTF-8 temporary files before replacement. CSV text is
neutralized against spreadsheet formula injection. Frozen `extra` provenance
is limited to `source`, `source_adapter`, query-free `feed_url`, and boolean
`active`; alumni annotations and other private fields are not exported.

## 2. Label without score anchoring

Open only the labels CSV while labeling. Do not inspect the predictions file
until labels are complete. The CSV contains posting context but intentionally
excludes fit score, watcher eligibility/action, predicted role track,
explanations, generic score, and degree decisions.

Human labels should reflect the user's real internship preferences, not an
attempt to guess or imitate the current watcher.

### Label rubric

`human_eligible`:

- `yes`: this posting belongs in the user's internship watcher.
- `no`: this posting should be excluded.
- `uncertain`: evidence is insufficient for a confident eligibility label;
  counted separately and excluded from binary and ranking metrics.

Every row requires one of those three values unless evaluation is explicitly
run with `--allow-partial-labels`. The remaining editable columns are optional
free text:

- `human_role_track`: the role category a human would assign.
- `human_exclusion_reason`: why a `no` posting should be excluded.
- `human_notes`: any other labeling context.

Do not edit `job_id`, `sample_groups`, or any job-information column.

## 3. Evaluate offline

PowerShell:

```powershell
$env:PYTHONPATH = ".;backend"

backend\venv\Scripts\python.exe scripts\evaluate_scoring_benchmark.py `
  --labels evaluation/private/scoring_20260720_labels.csv `
  --rows evaluation/private/scoring_20260720_rows.jsonl `
  --manifest evaluation/private/scoring_20260720_manifest.json `
  --baseline-predictions evaluation/private/scoring_20260720_predictions.json `
  --report evaluation/private/scoring_20260720_report.md `
  --metrics-json evaluation/private/scoring_20260720_metrics.json
```

POSIX:

```bash
PYTHONPATH=.:backend python3 scripts/evaluate_scoring_benchmark.py \
  --labels evaluation/private/scoring_20260720_labels.csv \
  --rows evaluation/private/scoring_20260720_rows.jsonl \
  --manifest evaluation/private/scoring_20260720_manifest.json \
  --baseline-predictions evaluation/private/scoring_20260720_predictions.json \
  --report evaluation/private/scoring_20260720_report.md \
  --metrics-json evaluation/private/scoring_20260720_metrics.json
```

The evaluator makes no network requests. It reruns the current repository's
`analyze_rows()` on the frozen JSONL using the manifest's exact `as_of_date`,
then joins by stable job ID. The frozen date matters because deadlines and
expiry decisions are date-sensitive. Missing, duplicate, or changed IDs fail
instead of silently changing the evaluated dataset.

By default every benchmark row must be present and `human_eligible` must be
`yes`, `no`, or `uncertain`. During labeling, `--allow-partial-labels` permits
blank labels or missing CSV rows and reports exact coverage. Optional free-text
fields never determine whether a row is complete.

## Metrics

Headline binary eligibility metrics—TP, FP, FN, TN, precision, recall,
specificity, accuracy, and F1—treat `yes` as eligible and `no` as ineligible,
using only those labels in the `random` cohort. `uncertain` and unlabeled rows
are excluded from every binary calculation.
The top-ranked and difficult cohorts are intentionally enriched and must not be
used for headline population precision or recall.

The report gives total `yes`, `no`, `uncertain`, and unlabeled counts. Across
all `yes`/`no` selected rows, it also includes:

- Eligibility Precision@10 and Precision@20 for baseline and current rankings.
- Current fit-score-band eligibility rates, false positives, and false
  negatives.
- False positives/negatives by predicted role track, human exclusion-reason
  counts, and predicted-versus-human role confusion.
- Deterministically ranked largest disagreements. Eligibility mismatches sort
  first; remaining disagreements use the absolute gap between fit score and
  the binary human target (`yes=100`, `no=0`). Fit score remains a ranking
  score, not a probability.
- Baseline/current changes to eligibility, fit score, role track, action, and
  degree eligibility.

Zero denominators are reported as `n/a` in Markdown and `null` in JSON.

## Comparing a later scoring version

Keep the original rows, labels, baseline predictions, and manifest together.
After changing scoring in a separately scoped task, rerun only the evaluator.
It compares the new repository output with both the frozen baseline and human
labels without recollecting live postings. Do not retune and relabel the same
benchmark repeatedly without keeping a separate holdout set.

## Privacy and source control

`evaluation/private/` is ignored and is the intended location for real posting
text, labels, predictions, manifests, reports, and metrics. These files may
contain full public posting descriptions and the user's private preference
labels; do not commit them or upload them as Actions artifacts.

This README and synthetic offline tests are safe to commit. The exporter does
not load private alumni data, and its explicit field whitelist prevents alumni
names, LinkedIn URLs, or roster details from entering benchmark outputs.

## U.S. role-fit benchmark

The historical `scoring_20260724_*` benchmark remains immutable as the
location-gate baseline. Use the separate U.S. role-fit exporter when measuring
role classification:

Before exporting, commit the completed benchmark-construction changes and
confirm `git status --short` has no tracked entries. A benchmark is frozen only
when its manifest records `git_dirty=false` and the exact commit being tested;
ignored files under `evaluation/private/` do not make the repository dirty.

```powershell
$env:WATCHER_SEND_EMAIL = "0"
$env:PYTHONPATH = ".;backend"

backend\venv\Scripts\python.exe scripts\build_us_rolefit_benchmark.py `
  --watchlist watcher\watchlist.yml `
  --as-of 2026-07-26 `
  --seed 20260726 `
  --output-prefix evaluation\private\scoring_us_rolefit_20260726
```

POSIX equivalent:

```bash
WATCHER_SEND_EMAIL=0 PYTHONPATH=.:backend python3 \
  scripts/build_us_rolefit_benchmark.py \
  --watchlist watcher/watchlist.yml \
  --as-of 2026-07-26 \
  --seed 20260726 \
  --output-prefix evaluation/private/scoring_us_rolefit_20260726
```

This exporter starts with the same open-internship population as the general
benchmark, then excludes only rows for which
`watcher.eligibility.assess_us_location()` returns `outside_us`. Explicit U.S.,
U.S.-remote, multi-location U.S., and location-ambiguous rows remain.
Production eligibility, classification, scores, actions, and ranking are not
changed.

The three independent cohorts are:

- `random`: 160 requested rows sampled from the complete U.S./ambiguous
  candidate pool. This remains the only headline population cohort.
- `likely_match`: 80 requested current model-positive rows, stratified across
  software and software-adjacent role tracks.
- `difficult_negative`: 80 requested rows, stratified across explicit
  engineering/nontechnical confusion patterns, graduate-only roles, and other
  current model-negative internships.

Rows may carry multiple group names but are emitted once by stable job ID. If a
cohort lacks enough candidates, all available rows are used and the manifest's
`target_shortfalls` records the limitation. The manifest also records
available/requested/actual cohort counts, overlaps, expected model positives,
source/company/role/location distributions, the frozen Git commit, and hashes.

The command creates blank labels, frozen rows, baseline predictions, a
manifest, and an initial report/metrics pair evaluated with partial labels.
The initial report therefore shows zero label coverage; it is only a structural
validation. Label only the CSV using `yes`, `no`, or `uncertain`, then rerun the
normal evaluator without `--allow-partial-labels` once every row is labeled.
For a same-prefix rebuild, snapshot the prior exact job IDs, cohort memberships,
and location statuses first, then explain all live-source differences.
