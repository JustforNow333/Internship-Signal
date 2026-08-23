"""Narrow lifecycle helpers for direct-source record parsing."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from watcher.config import CompanyCfg
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.parsing import parse_records


class DirectRecordAdapter(DirectDiagnosticsMixin):
    """Share only invariant record parsing and diagnostic plumbing."""

    name: str

    def _parse_direct_records(
        self,
        records: list,
        company: CompanyCfg,
        parse_record: Callable[[Any], dict],
        *,
        include: Callable[[Any], bool] | None = None,
    ) -> list[dict]:
        return parse_records(
            records,
            parse_record,
            source_name=self.name,
            company_name=company.name,
            include=include,
            diagnostics=self._record_parse_diagnostics,
        )


class SinglePayloadDirectAdapter(DirectRecordAdapter, ABC):
    """Parse one authoritative payload through the standard direct lifecycle."""

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        records = self._records_from_payload(payload, company)
        rows = self._parse_direct_records(
            records,
            company,
            lambda record: self._parse_record(record, company),
        )
        self._finish_direct_diagnostics(rows)
        return rows

    @abstractmethod
    def _records_from_payload(self, payload: Any, company: CompanyCfg) -> list:
        """Validate one provider payload and return its posting records."""

    @abstractmethod
    def _parse_record(self, record: Any, company: CompanyCfg) -> dict:
        """Map one provider record to a canonical row."""
