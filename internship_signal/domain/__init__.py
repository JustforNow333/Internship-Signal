"""Shared domain concepts owned by neither the backend nor the watcher.

`backend.app` and `watcher` both depend on this package; it depends on neither,
so the dependency direction is one-way:

    internship_signal.domain
          /        \\
     watcher      backend

Only small, dependency-light constants and pure functions that are genuinely
shared across that boundary belong here. Anything specific to one layer —
scoring, dedupe orchestration, persistence, APIs, source adapters, notification
— stays in that layer.
"""
