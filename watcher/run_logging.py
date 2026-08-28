"""Run-scoped logging: the shared watcher logger and stage timing."""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from typing import Iterator



# The logger name is pinned rather than derived from ``__name__``: the console
# format prints it, the replay benchmark filters records by it, and tests set
# levels on it, so every run-scoped record must keep reading ``watcher.run``.
LOGGER = logging.getLogger("watcher.run")


@contextmanager
def _timed_stage(stage: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        LOGGER.info(
            "STAGE-TIMING stage=%s seconds=%.3f",
            _timing_log_value(stage),
            time.perf_counter() - started,
        )


def _timing_log_value(value: object) -> str:
    text = re.sub(r"[\x00-\x20\x7f]+", "_", str(value or "").strip())
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_.-")
    text = re.sub(r"_+", "_", text)
    return text[:120] or "unknown"
