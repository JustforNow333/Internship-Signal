"""The source adapter interface: its protocol, errors, and response types.

The lowest adapter-facing layer. Transport, parsing, diagnostics, and row
construction all depend on this module; it depends on nothing in the source
layer except the sanitizers its exception messages need.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from watcher.config import CompanyCfg
from watcher.sources.sanitize import _safe_error_code, _sanitize_fetch_message


class SourceError(Exception):
    """Base class for catchable source adapter failures."""


class SourceFetchError(SourceError):
    """Raised when a source endpoint cannot be fetched or decoded."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "fetch_failure",
        status_code: int | None = None,
        retryable: bool = False,
        response_metadata: Mapping[str, object] | None = None,
        attempt_count: int = 1,
    ) -> None:
        super().__init__(_sanitize_fetch_message(message))
        self.error_code = _safe_error_code(error_code)
        self.status_code = status_code
        self.retryable = bool(retryable)
        self.response_metadata = dict(response_metadata or {})
        self.attempt_count = max(1, int(attempt_count))


class SourceSchemaError(SourceError):
    """Raised when a source response has an unexpected shape."""


class Source(Protocol):
    name: str

    def fetch(self, company: CompanyCfg) -> list[dict]:
        """Return canonical-shaped rows or raise SourceError on failure."""


@dataclass(frozen=True)
class JsonHttpResponse:
    """Decoded JSON plus safe response metadata for diagnostics and probes."""

    payload: Any
    metadata: Mapping[str, object]


@dataclass(frozen=True)
class TextHttpResponse:
    """Decoded UTF-8 text plus safe response metadata."""

    text: str
    metadata: Mapping[str, object]


def require_token(company: CompanyCfg, source_name: str) -> str:
    token = (company.token or "").strip()
    if not token:
        raise SourceError(f"{source_name} requires a token for {company.name}")
    return token
