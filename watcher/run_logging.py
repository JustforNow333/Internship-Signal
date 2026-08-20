"""Shared watcher-run logger and stable timing-log helpers.

Every module split out of the original ``watcher/run.py`` logs through
:data:`LOGGER`. The console format includes the logger name, and both the
scheduled workflow log and the replay benchmark tooling read records by that
exact name, so the name stays ``watcher.run`` regardless of which module emits
the record.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from typing import Iterator

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
