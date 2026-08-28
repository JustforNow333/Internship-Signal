"""Source-health monitoring, alert policy, persistence, rendering, and delivery.

The package is split by responsibility so one health change touches one module:

* :mod:`watcher.health.models` -- shared constants and dataclasses.
* :mod:`watcher.health.sanitize` -- the total sanitizers and UTC helpers.
* :mod:`watcher.health.state` -- health keys and per-attempt state transitions.
* :mod:`watcher.health.coverage` -- per-company coverage and GitHub row evidence.
* :mod:`watcher.health.store` -- the two SQLite stores.
* :mod:`watcher.health.policy` -- severity, fallback, flapping, and grouping.
* :mod:`watcher.health.rendering` -- alert, daily-summary, and digest text.
* :mod:`watcher.health.service` -- alert orchestration and the SMTP boundary.
* :mod:`watcher.health.report` -- the JSON artifact, workflow output, and CLI.

Nothing is re-exported here: import from the owning module, or from the
:mod:`watcher.source_health` and :mod:`watcher.health_alerts` compatibility
facades.
"""
