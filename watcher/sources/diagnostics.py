"""Bounded, payload-free health diagnostics published by direct adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from watcher.sources.sanitize import _safe_error_code


@dataclass(frozen=True)
class DirectSourceDiagnostics:
    """Bounded, payload-free diagnostics shared by every direct adapter."""

    attempted: bool = True
    succeeded: bool | None = None
    retained_row_count: int = 0
    malformed_row_count: int = 0
    schema_error_row_count: int = 0
    duplicate_row_count: int = 0
    failed_request_count: int = 0
    incomplete: bool = False
    truncated: bool = False
    reason_codes: tuple[str, ...] = ()
    degraded: bool = False
    complete: bool = False

    def __post_init__(self) -> None:
        for field in (
            "retained_row_count",
            "malformed_row_count",
            "schema_error_row_count",
            "duplicate_row_count",
            "failed_request_count",
        ):
            try:
                value = max(0, int(getattr(self, field)))
            except (TypeError, ValueError, OverflowError):
                value = 0
            object.__setattr__(self, field, min(1_000_000_000, value))
        reasons: list[str] = []
        for reason in self.reason_codes:
            raw = str(reason or "").strip()
            if not raw:
                continue
            code = _safe_error_code(raw)[:80]
            if code not in reasons:
                reasons.append(code)
            if len(reasons) >= 12:
                break
        object.__setattr__(self, "reason_codes", tuple(reasons))


class DirectDiagnosticsMixin:
    """Small helper for adapters that use the standard record parser."""

    last_health_diagnostics = DirectSourceDiagnostics(attempted=False)

    def _begin_direct_diagnostics(self) -> None:
        self._diagnostic_malformed_rows = 0
        self._diagnostic_schema_rows = 0
        self._diagnostic_reasons: list[str] = []
        self.last_health_diagnostics = DirectSourceDiagnostics()

    def _record_parse_diagnostics(
        self,
        malformed_rows: int,
        schema_error_rows: int,
        reason_codes: Iterable[str],
    ) -> None:
        self._diagnostic_malformed_rows += max(0, int(malformed_rows))
        self._diagnostic_schema_rows += max(0, int(schema_error_rows))
        for reason in reason_codes:
            code = _safe_error_code(reason)[:80]
            if code and code not in self._diagnostic_reasons:
                self._diagnostic_reasons.append(code)

    def _finish_direct_diagnostics(
        self,
        rows: list[dict],
        *,
        duplicate_row_count: int = 0,
        failed_request_count: int = 0,
        incomplete: bool = False,
        truncated: bool = False,
        degraded: bool | None = None,
        complete: bool | None = None,
        reason_codes: Iterable[str] = (),
    ) -> None:
        self._record_parse_diagnostics(0, 0, reason_codes)
        parse_loss = bool(
            self._diagnostic_malformed_rows or self._diagnostic_schema_rows
        )
        is_degraded = (
            parse_loss or incomplete or truncated
            if degraded is None
            else bool(degraded)
        )
        known_complete = (
            not (is_degraded or incomplete or truncated)
            if complete is None
            else bool(complete)
        )
        self.last_health_diagnostics = DirectSourceDiagnostics(
            succeeded=True,
            retained_row_count=len(rows),
            malformed_row_count=min(1_000_000_000, self._diagnostic_malformed_rows),
            schema_error_row_count=min(1_000_000_000, self._diagnostic_schema_rows),
            duplicate_row_count=min(1_000_000_000, max(0, int(duplicate_row_count))),
            failed_request_count=min(1_000_000_000, max(0, int(failed_request_count))),
            incomplete=bool(incomplete),
            truncated=bool(truncated),
            reason_codes=tuple(self._diagnostic_reasons[:12]),
            degraded=is_degraded,
            complete=known_complete,
        )
