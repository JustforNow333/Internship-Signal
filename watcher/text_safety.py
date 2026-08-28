"""Dependency-free conversion helpers for failure-path diagnostics."""

from __future__ import annotations


def safe_text(value: object) -> str:
    """Return text without trusting an arbitrary object's conversion hooks."""

    try:
        if not value:
            return ""
    except Exception:
        pass
    try:
        return str(value)
    except Exception:
        return ""


def exception_text(error: BaseException) -> str:
    """Return the normal ``Type: message`` shape even when ``str`` is broken."""

    return f"{type(error).__name__}: {safe_text(error)}"
