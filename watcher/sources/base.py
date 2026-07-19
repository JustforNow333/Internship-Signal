"""Shared source adapter interfaces and helpers."""

from __future__ import annotations

import hashlib
import gzip
import io
import json
import logging
import re
import socket
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from json import JSONDecodeError
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.app.normalize import CANONICAL_COLUMNS
from watcher.config import CompanyCfg

USER_AGENT = "internship-signal-watcher/0.1"
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SAFE_PREVIEW_CHARS = 160
LOGGER = logging.getLogger(__name__)


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


class _DecodedBodyTooLarge(ValueError):
    pass


class Source(Protocol):
    name: str

    def fetch(self, company: CompanyCfg) -> list[dict]:
        """Return canonical-shaped rows or raise SourceError on failure."""


@dataclass(frozen=True)
class JsonHttpResponse:
    """Decoded JSON plus safe response metadata for diagnostics and probes."""

    payload: Any
    metadata: Mapping[str, object]


def make_row(*, source: str, source_adapter: str, extra: dict | None = None, **fields: Any) -> dict:
    """Build a canonical-shaped row and attach source metadata.

    `source` is the source priority tag used later by merge/digest code
    ("direct" or "github"). `source_adapter` records the concrete adapter.
    """

    unknown = set(fields) - set(CANONICAL_COLUMNS)
    if unknown:
        raise ValueError(f"Unknown canonical fields: {', '.join(sorted(unknown))}")

    row = {column: "" for column in CANONICAL_COLUMNS}
    for key, value in fields.items():
        row[key] = "" if value is None else str(value)

    metadata = {"source": source, "source_adapter": source_adapter}
    if extra:
        metadata.update(extra)
    row["extra"] = metadata
    return row


def require_token(company: CompanyCfg, source_name: str) -> str:
    token = (company.token or "").strip()
    if not token:
        raise SourceError(f"{source_name} requires a token for {company.name}")
    return token


def fetch_json(url: str, source_name: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as exc:
        raise SourceFetchError(f"{source_name} fetch failed with HTTP {exc.code}: {url}") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise SourceFetchError(f"{source_name} fetch failed: {url}") from exc

    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError) as exc:
        raise SourceFetchError(f"{source_name} returned invalid JSON: {url}") from exc


def post_json(
    url: str,
    payload: dict,
    source_name: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    return post_json_response(
        url,
        payload,
        source_name,
        timeout=timeout,
    ).payload


def post_json_response(
    url: str,
    payload: dict,
    source_name: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    *,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    include_preview: bool = False,
    opener: Callable[..., Any] = urlopen,
) -> JsonHttpResponse:
    """POST JSON once and return decoded data with bounded safe metadata.

    Retries intentionally live in the Workday adapter so other adapters retain
    their existing semantics. This helper never records cookies, arbitrary
    headers, request headers, or a raw response body.
    """

    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with opener(request, timeout=timeout) as response:
            return _decode_json_http_response(
                response,
                request_url=url,
                source_name=source_name,
                max_response_bytes=max_response_bytes,
                include_preview=include_preview,
            )
    except HTTPError as exc:
        try:
            return _decode_json_http_response(
                exc,
                request_url=url,
                source_name=source_name,
                max_response_bytes=max_response_bytes,
                include_preview=include_preview,
            )
        except SourceFetchError:
            raise
        except Exception as diagnostic_exc:
            raise SourceFetchError(
                f"{source_name} POST failed with HTTP {exc.code}: {_safe_url(url)}",
                error_code=_http_error_code(exc.code),
                status_code=exc.code,
                retryable=exc.code == 429 or exc.code in {500, 502, 503, 504},
            ) from diagnostic_exc
    except (TimeoutError, URLError, OSError) as exc:
        code = _network_error_code(exc)
        raise SourceFetchError(
            f"{source_name} POST failed: code={code} endpoint={_safe_url(url)}",
            error_code=code,
            retryable=True,
        ) from exc


def _decode_json_http_response(
    response: Any,
    *,
    request_url: str,
    source_name: str,
    max_response_bytes: int,
    include_preview: bool,
) -> JsonHttpResponse:
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")

    status = int(getattr(response, "status", 0) or getattr(response, "code", 0) or 200)
    headers = getattr(response, "headers", None)
    content_type = _header_value(headers, "Content-Type")
    content_encoding = _header_value(headers, "Content-Encoding").casefold()
    final_url = _safe_url(_response_url(response, request_url))
    request_label = _safe_url(request_url)
    redirected = final_url != request_label
    raw_body = response.read(max_response_bytes + 1)
    if len(raw_body) > max_response_bytes:
        metadata = _response_metadata(
            status=status,
            final_url=final_url,
            content_type=content_type,
            content_encoding=content_encoding,
            raw_body=raw_body,
            body_kind="oversized",
            redirected=redirected,
            transient=False,
        )
        raise SourceFetchError(
            f"{source_name} response exceeded {max_response_bytes} bytes: endpoint={request_label}",
            error_code="response_too_large",
            status_code=status,
            retryable=False,
            response_metadata=metadata,
        )

    digest = hashlib.sha256(raw_body).hexdigest()
    try:
        decoded_body = _decode_content_encoding(
            raw_body,
            content_encoding,
            max_response_bytes=max_response_bytes,
        )
    except _DecodedBodyTooLarge as exc:
        metadata = _response_metadata(
            status=status,
            final_url=final_url,
            content_type=content_type,
            content_encoding=content_encoding,
            raw_body=raw_body,
            body_kind="oversized",
            redirected=redirected,
            transient=False,
            body_sha256=digest,
        )
        raise SourceFetchError(
            f"{source_name} decoded response exceeded {max_response_bytes} bytes: endpoint={request_label}",
            error_code="response_too_large",
            status_code=status,
            retryable=False,
            response_metadata=metadata,
        ) from exc
    except (gzip.BadGzipFile, OSError, zlib.error) as exc:
        metadata = _response_metadata(
            status=status,
            final_url=final_url,
            content_type=content_type,
            content_encoding=content_encoding,
            raw_body=raw_body,
            body_kind="compressed_error",
            redirected=redirected,
            transient=True,
            body_sha256=digest,
        )
        raise SourceFetchError(
            f"{source_name} response compression could not be decoded: endpoint={request_label}",
            error_code="compressed_decode_failure",
            status_code=status,
            retryable=True,
            response_metadata=metadata,
        ) from exc

    text, text_error = _decode_response_text(decoded_body, content_type)
    body_kind = _body_kind(decoded_body, text)
    metadata = _response_metadata(
        status=status,
        final_url=final_url,
        content_type=content_type,
        content_encoding=content_encoding,
        raw_body=raw_body,
        body_kind=body_kind,
        redirected=redirected,
        transient=False,
        body_sha256=digest,
        retry_after_seconds=_retry_after_seconds(_header_value(headers, "Retry-After")),
        preview=_safe_body_preview(text) if include_preview and text is not None else None,
    )

    if status < 200 or status >= 300:
        error_code, retryable = _classify_http_failure(status, body_kind)
        metadata["transient"] = retryable
        raise SourceFetchError(
            f"{source_name} POST failed: code={error_code} status={status} endpoint={request_label}",
            error_code=error_code,
            status_code=status,
            retryable=retryable,
            response_metadata=metadata,
        )

    if not decoded_body:
        metadata["transient"] = True
        raise SourceFetchError(
            f"{source_name} returned an empty response: endpoint={request_label}",
            error_code="empty_response",
            status_code=status,
            retryable=True,
            response_metadata=metadata,
        )

    if text_error is not None or text is None:
        metadata["transient"] = True
        raise SourceFetchError(
            f"{source_name} returned undecodable JSON text: endpoint={request_label}",
            error_code="json_decode_failure",
            status_code=status,
            retryable=True,
            response_metadata=metadata,
        ) from text_error

    try:
        parsed = json.loads(text.lstrip("\ufeff"))
    except JSONDecodeError as exc:
        if body_kind in {"html", "html_challenge"}:
            error_code = "redirected_to_html" if redirected else (
                "html_challenge" if body_kind == "html_challenge" else "html_response"
            )
        elif not _json_content_type(content_type):
            error_code = "unsupported_content_type"
        else:
            error_code = "json_decode_failure"
        metadata["transient"] = True
        raise SourceFetchError(
            f"{source_name} returned non-JSON content: code={error_code} endpoint={request_label}",
            error_code=error_code,
            status_code=status,
            retryable=True,
            response_metadata=metadata,
        ) from exc

    metadata["body_kind"] = "json"
    metadata["json_decoded"] = True
    return JsonHttpResponse(payload=parsed, metadata=metadata)


def _response_metadata(
    *,
    status: int,
    final_url: str,
    content_type: str,
    content_encoding: str,
    raw_body: bytes,
    body_kind: str,
    redirected: bool,
    transient: bool,
    body_sha256: str | None = None,
    retry_after_seconds: float | None = None,
    preview: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "status": status,
        "final_url": final_url,
        "content_type": content_type[:160],
        "content_encoding": content_encoding[:40],
        "body_bytes": len(raw_body),
        "body_kind": body_kind,
        "body_sha256": body_sha256 or hashlib.sha256(raw_body).hexdigest(),
        "redirected": redirected,
        "transient": transient,
        "json_decoded": False,
    }
    if retry_after_seconds is not None:
        metadata["retry_after_seconds"] = retry_after_seconds
    if preview:
        metadata["preview"] = preview
    return metadata


def _decode_content_encoding(
    body: bytes,
    content_encoding: str,
    *,
    max_response_bytes: int,
) -> bytes:
    encodings = [item.strip() for item in content_encoding.split(",") if item.strip()]
    decoded = body
    for encoding in reversed(encodings):
        if encoding in {"", "identity"}:
            continue
        if encoding in {"gzip", "x-gzip"}:
            with gzip.GzipFile(fileobj=io.BytesIO(decoded)) as stream:
                decoded = stream.read(max_response_bytes + 1)
        elif encoding == "deflate":
            decoded = zlib.decompressobj().decompress(decoded, max_response_bytes + 1)
        else:
            raise zlib.error(f"unsupported content encoding: {encoding}")
        if len(decoded) > max_response_bytes:
            raise _DecodedBodyTooLarge
    return decoded


def _decode_response_text(body: bytes, content_type: str) -> tuple[str | None, Exception | None]:
    charset = _content_charset(content_type)
    if body.startswith(b"\xef\xbb\xbf"):
        charset = "utf-8-sig"
    try:
        return body.decode(charset), None
    except (LookupError, UnicodeDecodeError) as exc:
        return None, exc


def _content_charset(content_type: str) -> str:
    match = re.search(r"(?i)(?:^|;)\s*charset\s*=\s*[\"']?([^;\"']+)", content_type)
    charset = (match.group(1).strip().casefold() if match else "utf-8")
    aliases = {
        "utf8": "utf-8",
        "utf-8": "utf-8",
        "utf-8-sig": "utf-8-sig",
        "iso-8859-1": "iso-8859-1",
        "latin-1": "iso-8859-1",
        "windows-1252": "windows-1252",
        "cp1252": "windows-1252",
    }
    return aliases.get(charset, "utf-8")


def _body_kind(body: bytes, text: str | None) -> str:
    if not body:
        return "empty"
    if text is None:
        return "binary"
    lowered = text[:16_384].casefold()
    looks_html = bool(re.search(r"<!doctype\s+html|<html\b|<head\b|<body\b", lowered))
    if looks_html and any(marker in lowered for marker in _HTML_CHALLENGE_MARKERS):
        return "html_challenge"
    if looks_html:
        return "html"
    return "text"


_HTML_CHALLENGE_MARKERS = (
    "access denied",
    "captcha",
    "challenge",
    "checking your browser",
    "enable cookies",
    "enable javascript",
    "request blocked",
    "security check",
    "verify you are human",
)


def _classify_http_failure(status: int, body_kind: str) -> tuple[str, bool]:
    if status == 429:
        return "rate_limited", True
    if status in {500, 502, 503, 504}:
        return "transient_http_error", True
    if status == 403 and body_kind == "html_challenge":
        return "html_challenge", True
    return "permanent_http_error", False


def _http_error_code(status: int) -> str:
    return _classify_http_failure(int(status), "text")[0]


def _network_error_code(exc: BaseException) -> str:
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, ConnectionResetError):
        return "connection_reset"
    if isinstance(reason, socket.gaierror):
        return "dns_failure"
    return "network_failure"


def _retry_after_seconds(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _safe_body_preview(text: str) -> str:
    preview = html_to_text(text[:4_096])
    preview = re.sub(r"https?://[^\s]+", "[url-redacted]", preview, flags=re.IGNORECASE)
    preview = re.sub(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", "[email-redacted]", preview)
    preview = re.sub(
        r"(?i)\b(?:token|secret|password|passwd|authorization|api[_-]?key|csrf)\s*[:=]\s*[^\s,;]+",
        "[secret-redacted]",
        preview,
    )
    preview = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        "[id-redacted]",
        preview,
        flags=re.IGNORECASE,
    )
    preview = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "[id-redacted]", preview)
    preview = re.sub(r"\s+", " ", preview).strip()
    return preview[:MAX_SAFE_PREVIEW_CHARS]


def _response_url(response: Any, fallback: str) -> str:
    try:
        return str(response.geturl() or fallback)
    except (AttributeError, TypeError, ValueError):
        return fallback


def _header_value(headers: Any, name: str) -> str:
    if headers is None:
        return ""
    try:
        return str(headers.get(name, "") or "").strip()
    except (AttributeError, TypeError, ValueError):
        return ""


def _json_content_type(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().casefold()
    return media_type == "application/json" or media_type.endswith("+json")


def _safe_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() in {"http", "https"} and parsed.hostname:
        host = parsed.hostname
        try:
            if parsed.port:
                host = f"{host}:{parsed.port}"
        except ValueError:
            pass
        return urlunsplit((parsed.scheme.casefold(), host, parsed.path or "/", "", ""))
    return re.sub(r"[?#].*$", "", raw)


def _sanitize_fetch_message(value: object) -> str:
    message = re.sub(r"https?://[^\s]+", lambda match: _safe_url(match.group(0)), str(value or ""))
    message = re.sub(
        r"(?i)\b(?:password|passwd|token|secret|authorization|api[_-]?key|csrf)\s*[:=]\s*[^\s,;]+",
        "[secret-redacted]",
        message,
    )
    message = re.sub(r"[\x00-\x1f\x7f]+", " ", message)
    return re.sub(r"\s+", " ", message).strip()[:320]


def _safe_error_code(value: object) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "_", str(value or "fetch_failure").casefold()).strip("_") or "fetch_failure"


def html_to_text(value: Any) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|li|h[1-6])\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def iso_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()

    raw = str(value).strip()
    if not raw:
        return ""
    try:
        normalized = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        return raw[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", raw) else raw


def ensure_list(value: Any, source_name: str, field: str) -> list:
    if not isinstance(value, list):
        raise SourceSchemaError(f"{source_name} expected {field} to be a list")
    return value


def parse_records(
    records: list,
    parse_record: Callable[[Any], dict],
    *,
    source_name: str,
    company_name: str,
    include: Callable[[Any], bool] | None = None,
) -> list[dict]:
    """Retain valid direct-source records while rejecting all-malformed payloads."""

    candidates = [record for record in records if include is None or include(record)]
    rows: list[dict] = []
    skipped = 0
    for record in candidates:
        try:
            rows.append(parse_record(record))
        except SourceSchemaError:
            skipped += 1
    if skipped:
        safe_company = re.sub(r"[\x00-\x1f\x7f]+", " ", str(company_name or "unknown")).strip()[:120]
        LOGGER.warning(
            "Skipped %d malformed %s record(s) for %s; %d valid record(s) retained.",
            skipped,
            source_name,
            safe_company or "unknown",
            len(rows),
        )
    if candidates and not rows:
        raise SourceSchemaError(
            f"{source_name} received {len(candidates)} posting record(s) but none were valid"
        )
    return rows


def page_fingerprint(records: list) -> str:
    """Return a bounded digest used to detect broken repeated pagination pages."""

    encoded = json.dumps(records, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
