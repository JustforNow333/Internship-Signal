# Watcher spec — index

The original build spec has been split by purpose. Its still-current
requirements now live in the documents below; its completed build order and
superseded narrative live in the history archive.

| Former spec section | Now in |
|---|---|
| §0–§2 `analyze_rows` seam, architecture, code layout | [`docs/architecture.md`](docs/architecture.md) |
| §3 watchlist config | [`docs/watcher.md`](docs/watcher.md#2-watchlist-configuration) |
| §4 source adapters, Workday, backstops, season | [`docs/watcher-sources.md`](docs/watcher-sources.md), [`docs/watcher.md`](docs/watcher.md#3-source-adapters) |
| §5 merge, dedupe, source priority | [`docs/watcher.md`](docs/watcher.md#7-merge-dedupe-and-posting-identity) |
| §6 seen store | [`docs/watcher.md`](docs/watcher.md#9-digest-and-notification-state) |
| §7 filters | [`docs/watcher.md`](docs/watcher.md#8-eligibility-and-filtering) |
| §8 alumni join | [`docs/watcher.md`](docs/watcher.md#10-alumni-matching) |
| §9 email digest | [`docs/watcher.md`](docs/watcher.md#9-digest-and-notification-state) |
| §10 scheduler | [`docs/operations.md`](docs/operations.md) |
| §11 testing | [`docs/testing.md`](docs/testing.md) |
| §12 build order | [`docs/history/watcher-progress-archive.md`](docs/history/watcher-progress-archive.md) (complete) |
| §13 constraints | [`docs/watcher.md`](docs/watcher.md#18-standing-constraints) |
| §14 source health and coverage audit | [`docs/watcher.md`](docs/watcher.md#14-source-health) |
| §15 static-analysis cache | [`docs/watcher.md`](docs/watcher.md#12-persistent-static-analysis-cache) |
| §16 collection snapshot and replay | [`docs/watcher.md`](docs/watcher.md#13-collection-snapshot-and-replay) |
| §17 bounded source comparison | [`docs/watcher.md`](docs/watcher.md#16-posting-audit-and-source-comparison) |
| §18 bounded collection concurrency | [`docs/watcher.md`](docs/watcher.md#11-bounded-collection-concurrency), [`docs/operations.md`](docs/operations.md#8-concurrency-canaries-and-promotion) |

Start from [`AGENTS.md`](AGENTS.md). Do not add new specification text to this
file.
