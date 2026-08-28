"""Runnable watcher collection, analysis, digest, and seen-store core.

This module is the stable entry point (``python -m watcher.run``) and the
compatibility surface for everything that already imports from ``watcher.run``.
The implementation lives in focused modules:

* :mod:`watcher.collection` -- adapter resolution, fetch planning and outcomes,
  source attempts, and Workday transport counters.
* :mod:`watcher.pipeline` -- :func:`run_once` stage orchestration and
  :class:`RunResult`.
* :mod:`watcher.reporting` -- console report, application heartbeat, and the
  sanitized health JSON report.
* :mod:`watcher.cli` -- argument parsing and process startup.
* :mod:`watcher.run_logging` -- the shared ``watcher.run`` logger and the
  stable ``STAGE-TIMING`` helper.

Patch implementations where they are defined rather than through this module:
re-exported names are bound here at import time, so replacing one of them here
does not change what the owning module calls.
"""

from __future__ import annotations

from watcher.cli import main
from watcher.collection import (
    WORKDAY_TRANSPORT_ERROR_CODES,
    CollectionStats,
    WorkdayTransportSummary,
    _default_direct_sources,
    _direct_diagnostics_from_source,
    _DirectFetchOutcome,
    _direct_outcome_from_result,
    _DirectSourceProvider,
    _GithubFetchOutcome,
    _http_status_from_error,
    collect_batch,
    collect_rows,
    summarize_workday_transport,
)
from watcher.pipeline import (
    RUN_MODE_DRY,
    RUN_MODE_LIVE,
    RUN_MODE_PRIME,
    RUN_MODES,
    RunResult,
    run_once,
)
from watcher.reporting import print_heartbeat, print_report
from watcher.run_logging import LOGGER

# The private names above are re-exported, not part of the public surface:
# adapter, registry, and concurrency tests import `_default_direct_sources`,
# `_DirectSourceProvider`, `_DirectFetchOutcome`, `_direct_outcome_from_result`,
# `_direct_diagnostics_from_source`, and `_http_status_from_error` from here.

__all__ = [
    "LOGGER",
    "RUN_MODES",
    "RUN_MODE_DRY",
    "RUN_MODE_LIVE",
    "RUN_MODE_PRIME",
    "WORKDAY_TRANSPORT_ERROR_CODES",
    "CollectionStats",
    "RunResult",
    "WorkdayTransportSummary",
    "collect_batch",
    "collect_rows",
    "main",
    "print_heartbeat",
    "print_report",
    "run_once",
    "summarize_workday_transport",
]


if __name__ == "__main__":
    raise SystemExit(main())
