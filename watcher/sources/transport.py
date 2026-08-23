"""Bounded HTTP transport for source adapters.

Owns request construction, response decoding, decompression, charset handling,
and the classification of an HTTP or network failure into a `SourceFetchError`
with safe, payload-free response metadata. Retry policy is not here: it lives
in `watcher/sources/retry.py` and in the Workday adapter.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import socket
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from json import JSONDecodeError
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from watcher.sources.contracts import (
    JsonHttpResponse,
    SourceFetchError,
    TextHttpResponse,
)
from watcher.sources.sanitize import _safe_body_preview, _safe_url

USER_AGENT = "internship-signal-watcher/0.1"
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class _DecodedBodyTooLarge(ValueError):
    pass


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


def fetch_text(
    url: str,
    source_name: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    *,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> str:
    """Fetch one explicitly UTF-8 text response with a bounded body."""

    request = Request(
        url,
        headers={
            "Accept": "text/plain, text/markdown;q=0.9",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(max_response_bytes + 1)
    except HTTPError as exc:
        raise SourceFetchError(
            f"{source_name} fetch failed with HTTP {exc.code}: {_safe_url(url)}"
        ) from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise SourceFetchError(f"{source_name} fetch failed: {_safe_url(url)}") from exc
    if len(body) > max_response_bytes:
        raise SourceFetchError(
            f"{source_name} response exceeded {max_response_bytes} bytes: {_safe_url(url)}"
        )
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceFetchError(
            f"{source_name} returned non-UTF-8 text: {_safe_url(url)}"
        ) from exc


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
                request_method="POST",
            )
    except HTTPError as exc:
        try:
            return _decode_json_http_response(
                exc,
                request_url=url,
                source_name=source_name,
                max_response_bytes=max_response_bytes,
                include_preview=include_preview,
                request_method="POST",
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


def get_json_response(
    url: str,
    source_name: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    *,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    include_preview: bool = False,
    request_headers: Mapping[str, str] | None = None,
    opener: Callable[..., Any] = urlopen,
) -> JsonHttpResponse:
    """GET JSON once with the same bounded transport diagnostics as POST."""

    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    headers.update(request_headers or {})
    request = Request(
        url,
        headers=headers,
        method="GET",
    )
    try:
        with opener(request, timeout=timeout) as response:
            return _decode_json_http_response(
                response,
                request_url=url,
                source_name=source_name,
                max_response_bytes=max_response_bytes,
                include_preview=include_preview,
                request_method="GET",
            )
    except HTTPError as exc:
        try:
            return _decode_json_http_response(
                exc,
                request_url=url,
                source_name=source_name,
                max_response_bytes=max_response_bytes,
                include_preview=include_preview,
                request_method="GET",
            )
        except SourceFetchError:
            raise
        except Exception as diagnostic_exc:
            raise SourceFetchError(
                f"{source_name} GET failed with HTTP {exc.code}: {_safe_url(url)}",
                error_code=_http_error_code(exc.code),
                status_code=exc.code,
                retryable=exc.code == 429 or exc.code in {500, 502, 503, 504},
            ) from diagnostic_exc
    except (TimeoutError, URLError, OSError) as exc:
        code = _network_error_code(exc)
        raise SourceFetchError(
            f"{source_name} GET failed: code={code} endpoint={_safe_url(url)}",
            error_code=code,
            retryable=True,
        ) from exc


def get_text_response(
    url: str,
    source_name: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    *,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    opener: Callable[..., Any] = urlopen,
) -> TextHttpResponse:
    """GET UTF-8 HTML/text once with bounded, retry-aware diagnostics."""

    request = Request(
        url,
        headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with opener(request, timeout=timeout) as response:
            return _decode_text_http_response(
                response,
                request_url=url,
                source_name=source_name,
                max_response_bytes=max_response_bytes,
            )
    except HTTPError as exc:
        return _decode_text_http_response(
            exc,
            request_url=url,
            source_name=source_name,
            max_response_bytes=max_response_bytes,
        )
    except (TimeoutError, URLError, OSError) as exc:
        code = _network_error_code(exc)
        raise SourceFetchError(
            f"{source_name} GET failed: code={code} endpoint={_safe_url(url)}",
            error_code=code,
            retryable=True,
        ) from exc


def _decode_text_http_response(
    response: Any,
    *,
    request_url: str,
    source_name: str,
    max_response_bytes: int,
) -> TextHttpResponse:
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")
    status = int(getattr(response, "status", 0) or getattr(response, "code", 0) or 200)
    headers = getattr(response, "headers", None)
    content_type = _header_value(headers, "Content-Type")
    content_encoding = _header_value(headers, "Content-Encoding").casefold()
    final_url = _safe_url(_response_url(response, request_url))
    request_label = _safe_url(request_url)
    raw_body = response.read(max_response_bytes + 1)
    if len(raw_body) > max_response_bytes:
        raise SourceFetchError(
            f"{source_name} response exceeded {max_response_bytes} bytes: endpoint={request_label}",
            error_code="response_too_large",
            status_code=status,
        )
    try:
        decoded_body = _decode_content_encoding(
            raw_body,
            content_encoding,
            max_response_bytes=max_response_bytes,
        )
    except (_DecodedBodyTooLarge, gzip.BadGzipFile, OSError, zlib.error) as exc:
        raise SourceFetchError(
            f"{source_name} response could not be decoded: endpoint={request_label}",
            error_code="compressed_decode_failure",
            status_code=status,
            retryable=True,
        ) from exc
    text, text_error = _decode_response_text(decoded_body, content_type)
    body_kind = _body_kind(decoded_body, text)
    # HTML is the expected representation for this helper. A normal job page
    # may use the word "challenge" in its copy, so require a concrete access
    # interstitial phrase before classifying a successful HTML page as one.
    if (
        body_kind == "html_challenge"
        and text is not None
        and not _is_access_challenge_text(text)
    ):
        body_kind = "html"
    metadata = _response_metadata(
        status=status,
        final_url=final_url,
        content_type=content_type,
        content_encoding=content_encoding,
        raw_body=raw_body,
        body_kind=body_kind,
        redirected=final_url != request_label,
        transient=False,
        retry_after_seconds=_retry_after_seconds(_header_value(headers, "Retry-After")),
    )
    if status < 200 or status >= 300:
        error_code, retryable = _classify_http_failure(status, body_kind)
        metadata["transient"] = retryable
        raise SourceFetchError(
            f"{source_name} GET failed: code={error_code} status={status} endpoint={request_label}",
            error_code=error_code,
            status_code=status,
            retryable=retryable,
            response_metadata=metadata,
        )
    if not decoded_body:
        raise SourceFetchError(
            f"{source_name} returned an empty response: endpoint={request_label}",
            error_code="empty_response",
            status_code=status,
            retryable=True,
            response_metadata=metadata,
        )
    if text_error is not None or text is None:
        raise SourceFetchError(
            f"{source_name} returned undecodable text: endpoint={request_label}",
            error_code="text_decode_failure",
            status_code=status,
            retryable=False,
            response_metadata=metadata,
        ) from text_error
    if body_kind == "html_challenge":
        raise SourceFetchError(
            f"{source_name} returned an access challenge: endpoint={request_label}",
            error_code="html_challenge",
            status_code=status,
            retryable=False,
            response_metadata=metadata,
        )
    return TextHttpResponse(text=text.lstrip("\ufeff"), metadata=metadata)


def _decode_json_http_response(
    response: Any,
    *,
    request_url: str,
    source_name: str,
    max_response_bytes: int,
    include_preview: bool,
    request_method: str = "POST",
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
            f"{source_name} {request_method} failed: code={error_code} status={status} endpoint={request_label}",
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


def _is_access_challenge_text(text: str) -> bool:
    lowered = text[:16_384].casefold()
    return any(
        marker in lowered
        for marker in (
            "access denied",
            "captcha",
            "checking your browser",
            "request blocked",
            "security check",
            "verify you are human",
        )
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
