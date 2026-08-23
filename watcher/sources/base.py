"""Compatibility facade over the source layer.

The source layer is split by responsibility:

| Module | Owns |
|---|---|
| `sanitize.py` | total text sanitizers for URLs, error codes, messages, previews |
| `contracts.py` | `Source`, the `SourceError` family, HTTP response types |
| `diagnostics.py` | `DirectSourceDiagnostics` and `DirectDiagnosticsMixin` |
| `transport.py` | HTTP requests, decoding, and failure classification |
| `parsing.py` | `parse_records` and payload-shape validation |
| `rows.py` | canonical row construction and date normalization |
| `retry.py` | the shared bounded retry primitive |

This module re-exports that surface unchanged so existing
`from watcher.sources.base import ...` imports keep working. New adapter code
should import from the specific module instead.
"""

from __future__ import annotations

from watcher.sources.contracts import (
    JsonHttpResponse,
    Source,
    SourceError,
    SourceFetchError,
    SourceSchemaError,
    TextHttpResponse,
    require_token,
)
from watcher.sources.diagnostics import (
    DirectDiagnosticsMixin,
    DirectSourceDiagnostics,
)
from watcher.sources.parsing import (
    ensure_list,
    page_fingerprint,
    parse_records,
)
from watcher.sources.rows import (
    iso_date,
    make_row,
)
from watcher.sources.sanitize import (
    MAX_SAFE_PREVIEW_CHARS,
    _safe_body_preview,
    _safe_error_code,
    _safe_url,
    _sanitize_fetch_message,
    html_to_text,
)
from watcher.sources.transport import (
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    USER_AGENT,
    _body_kind,
    _classify_http_failure,
    _content_charset,
    _decode_content_encoding,
    _decode_json_http_response,
    _decode_response_text,
    _decode_text_http_response,
    _DecodedBodyTooLarge,
    _header_value,
    _http_error_code,
    _is_access_challenge_text,
    _json_content_type,
    _network_error_code,
    _response_metadata,
    _response_url,
    _retry_after_seconds,
    fetch_json,
    fetch_text,
    get_json_response,
    get_text_response,
    post_json,
    post_json_response,
)

__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "DirectDiagnosticsMixin",
    "DirectSourceDiagnostics",
    "JsonHttpResponse",
    "MAX_SAFE_PREVIEW_CHARS",
    "Source",
    "SourceError",
    "SourceFetchError",
    "SourceSchemaError",
    "TextHttpResponse",
    "USER_AGENT",
    "ensure_list",
    "fetch_json",
    "fetch_text",
    "get_json_response",
    "get_text_response",
    "html_to_text",
    "iso_date",
    "make_row",
    "page_fingerprint",
    "parse_records",
    "post_json",
    "post_json_response",
    "require_token",
]
