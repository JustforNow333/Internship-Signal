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
  excluded from binary and ranking metrics.

`human_priority` (required for `yes`; optional for `no`, where blank is treated
as zero for aggregate ranking diagnostics):

- `4`: excellent fit, clearly apply.
- `3`: good fit, worth applying.
- `2`: borderline, investigate.
- `1`: technically relevant but weak.
- `0`: irrelevant.

`human_action` is required for `yes` and `no`:

- `apply_now`
- `apply_later`
- `research_more`
- `skip`

`human_role_track`, `error_category`, and `label_notes` are optional free-text
diagnostics. Do not edit `job_id` or `sample_groups`.

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

By default every row must have a complete label, except that `uncertain` needs
no action or priority. During labeling, `--allow-partial-labels` permits an
interim report over complete, non-uncertain rows and reports exact coverage.

## Metrics

Headline binary eligibility metrics—TP, FP, FN, TN, precision, recall,
specificity, accuracy, and F1—use only fully labeled `random` cohort rows.
The top-ranked and difficult cohorts are intentionally enriched and must not be
used for headline population precision or recall.

Across all fully labeled, non-uncertain selected rows, the report includes:

- Precision@10 and Precision@20, where a good result is human eligible with
  priority at least 3.
- Average human priority in the same top-k cutoffs.
- The same ranking metrics for baseline and current predictions.
- Current fit-score-band eligibility rates, priorities, false positives, and
  false negatives.
- False positives/negatives by predicted role track, human error-category
  counts, and predicted-versus-human role confusion.
- Deterministically ranked largest disagreements. Eligibility mismatches sort
  first; remaining disagreements use the absolute gap between fit score and
  priority mapped to 0/25/50/75/100. Fit score remains a ranking score, not a
  probability.
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
