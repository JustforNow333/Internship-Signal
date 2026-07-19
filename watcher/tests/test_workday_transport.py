import hashlib
import gzip
import io
import logging
from email.message import Message
from urllib.error import URLError

import pytest

from watcher.config import CompanyCfg, ConfigError, workday_min_interval_seconds
from watcher.sources.base import SourceFetchError, SourceSchemaError, post_json_response
from watcher.sources.workday import WorkdayPacer, WorkdaySource, workday_retry_delay


URL = "https://tenant.wd5.myworkdayjobs.com/wday/cxs/tenant/Site/jobs?secret=hidden"


class FakeResponse:
    def __init__(self, body, *, status=200, content_type="application/json", encoding="", url=URL, headers=None):
        self._body = body
        self.status = status
        self.code = status
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if encoding:
            self.headers["Content-Encoding"] = encoding
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self, size=-1):
        return self._body if size < 0 else self._body[:size]

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def response_call(body, **kwargs):
    response = FakeResponse(body, **kwargs)
    return post_json_response(URL, {}, "workday", opener=lambda request, timeout: response)


def workday_company(name="Tenant"):
    return CompanyCfg(
        name=name,
        ats="workday",
        token="tenant",
        workday_shard="wd5",
        workday_site="Site",
    )


def transient_error(code="html_challenge", *, status=200, metadata=None):
    return SourceFetchError(
        "safe transient failure",
        error_code=code,
        status_code=status,
        retryable=True,
        response_metadata=metadata or {"body_kind": code, "body_sha256": "a" * 64},
    )


def test_valid_json_with_application_json_returns_safe_metadata():
    result = response_call(b'{"jobPostings": [], "total": 0}')

    assert result.payload["jobPostings"] == []
    assert result.metadata["status"] == 200
    assert result.metadata["body_kind"] == "json"
    assert result.metadata["json_decoded"] is True
    assert "preview" not in result.metadata


def test_valid_json_with_misleading_content_type_is_accepted():
    result = response_call(b'{"jobPostings": []}', content_type="text/plain")

    assert result.payload == {"jobPostings": []}


def test_html_200_is_classified_without_raw_body():
    marker = "PRIVATE_RAW_HTML_MARKER"
    with pytest.raises(SourceFetchError) as exc_info:
        response_call(f"<html><body>{marker}</body></html>".encode(), content_type="text/html")

    error = exc_info.value
    assert error.error_code == "html_response"
    assert error.retryable is True
    assert error.response_metadata["body_kind"] == "html"
    assert marker not in str(error)
    assert "preview" not in error.response_metadata


def test_html_challenge_markers_are_generic_and_retryable():
    with pytest.raises(SourceFetchError) as exc_info:
        response_call(b"<html><body>Security check: verify you are human</body></html>", content_type="text/html")

    assert exc_info.value.error_code == "html_challenge"
    assert exc_info.value.response_metadata["body_kind"] == "html_challenge"


def test_empty_http_200_is_retryable():
    with pytest.raises(SourceFetchError) as exc_info:
        response_call(b"")

    assert exc_info.value.error_code == "empty_response"
    assert exc_info.value.retryable is True


def test_utf8_bom_json_decodes():
    result = response_call(b"\xef\xbb\xbf" + b'{"jobPostings": []}')

    assert result.payload == {"jobPostings": []}


def test_gzip_json_decodes_without_advertising_brotli():
    body = gzip.compress(b'{"jobPostings": [], "total": 0}')

    result = response_call(body, encoding="gzip")

    assert result.payload["total"] == 0
    assert result.metadata["content_encoding"] == "gzip"


def test_invalid_or_unsupported_compression_is_classified():
    for encoding in ("gzip", "br"):
        with pytest.raises(SourceFetchError) as exc_info:
            response_call(b"not-compressed", encoding=encoding)
        assert exc_info.value.error_code == "compressed_decode_failure"
        assert exc_info.value.retryable is True


def test_invalid_utf8_is_classified_without_decoded_preview():
    with pytest.raises(SourceFetchError) as exc_info:
        response_call(b"\xff\xfe\xfa", content_type="application/json; charset=utf-8")

    assert exc_info.value.error_code == "json_decode_failure"
    assert exc_info.value.response_metadata["body_kind"] == "binary"


def test_oversized_response_fails_without_reading_unbounded_data():
    with pytest.raises(SourceFetchError) as exc_info:
        post_json_response(
            URL,
            {},
            "workday",
            max_response_bytes=8,
            opener=lambda request, timeout: FakeResponse(b"1234567890"),
        )

    assert exc_info.value.error_code == "response_too_large"
    assert exc_info.value.retryable is False
    assert exc_info.value.response_metadata["body_bytes"] == 9


def test_redirect_to_html_is_classified_and_final_url_has_no_query():
    with pytest.raises(SourceFetchError) as exc_info:
        response_call(
            b"<html><body>maintenance</body></html>",
            content_type="text/html",
            url="https://tenant.wd5.myworkdayjobs.com/refresh?token=private",
        )

    assert exc_info.value.error_code == "redirected_to_html"
    assert exc_info.value.response_metadata["final_url"].endswith("/refresh")
    assert "private" not in str(exc_info.value.response_metadata)


def test_error_sanitization_removes_request_query_strings():
    with pytest.raises(SourceFetchError) as exc_info:
        response_call(b"not json")

    assert "secret" not in str(exc_info.value)
    assert "hidden" not in str(exc_info.value)


def test_opt_in_preview_is_bounded_tag_stripped_and_redacted():
    body = (
        "<html><body>token=supersecret user@example.com "
        "https://example.test/path?secret=value 123e4567-e89b-12d3-a456-426614174000 "
        + "x" * 500
        + "</body></html>"
    ).encode()
    with pytest.raises(SourceFetchError) as exc_info:
        post_json_response(
            URL,
            {},
            "workday",
            include_preview=True,
            opener=lambda request, timeout: FakeResponse(body, content_type="text/html"),
        )

    preview = exc_info.value.response_metadata["preview"]
    assert len(preview) <= 160
    assert "<html" not in preview
    assert "supersecret" not in preview
    assert "user@example.com" not in preview
    assert "secret=value" not in preview
    assert "123e4567" not in preview


def test_body_hash_is_stable_and_uses_full_bounded_body():
    body = b"<html>stable</html>"
    errors = []
    for _ in range(2):
        with pytest.raises(SourceFetchError) as exc_info:
            response_call(body, content_type="text/html")
        errors.append(exc_info.value)

    assert errors[0].response_metadata["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert errors[0].response_metadata["body_sha256"] == errors[1].response_metadata["body_sha256"]


def test_cookies_sensitive_headers_and_raw_html_are_never_exposed(caplog):
    marker = "RAW_CHALLENGE_PAYLOAD"
    response = FakeResponse(
        f"<html><body>Access denied {marker}</body></html>".encode(),
        content_type="text/html",
        headers={"Set-Cookie": "session=TOPSECRET", "X-Csrf-Token": "CSRFSECRET"},
    )
    captured_headers = {}

    def opener(request, timeout):
        captured_headers.update(dict(request.header_items()))
        return response

    with pytest.raises(SourceFetchError) as exc_info:
        post_json_response(URL, {}, "workday", opener=opener)

    combined = str(exc_info.value) + repr(exc_info.value.response_metadata) + caplog.text
    assert "Set-Cookie" not in combined
    assert "TOPSECRET" not in combined
    assert "CSRFSECRET" not in combined
    assert marker not in combined
    assert "Cookie" not in captured_headers
    assert "Authorization" not in captured_headers


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_transient_http_statuses_are_retryable(status):
    with pytest.raises(SourceFetchError) as exc_info:
        response_call(b"temporary", status=status)

    assert exc_info.value.error_code == "transient_http_error"
    assert exc_info.value.retryable is True


def test_http_429_is_classified_with_retry_after_metadata():
    with pytest.raises(SourceFetchError) as exc_info:
        response_call(b"slow down", status=429, headers={"Retry-After": "7"})

    assert exc_info.value.error_code == "rate_limited"
    assert exc_info.value.retryable is True
    assert exc_info.value.response_metadata["retry_after_seconds"] == 7


@pytest.mark.parametrize("status", [400, 401, 404])
def test_permanent_http_statuses_are_not_retryable(status):
    with pytest.raises(SourceFetchError) as exc_info:
        response_call(b"bad request", status=status)

    assert exc_info.value.error_code == "permanent_http_error"
    assert exc_info.value.retryable is False


def test_403_html_challenge_is_retryable_but_plain_403_is_not():
    with pytest.raises(SourceFetchError) as challenge:
        response_call(b"<html>Security check: enable cookies</html>", status=403, content_type="text/html")
    with pytest.raises(SourceFetchError) as plain:
        response_call(b"forbidden", status=403, content_type="text/plain")

    assert challenge.value.error_code == "html_challenge"
    assert challenge.value.retryable is True
    assert plain.value.error_code == "permanent_http_error"
    assert plain.value.retryable is False


def test_http_429_retries_then_succeeds_without_real_sleep():
    calls = []
    sleeps = []

    def request_json(url, payload, source_name):
        calls.append(url)
        if len(calls) == 1:
            raise transient_error("rate_limited", status=429)
        return {"jobPostings": [], "total": 0}

    source = WorkdaySource(
        min_interval_seconds=0,
        request_json=request_json,
        sleeper=sleeps.append,
        jitter=lambda low, high: low,
    )

    assert source.fetch(workday_company()) == []
    assert len(calls) == 2
    assert sleeps == [1.0]
    assert source.last_diagnostics.retry_attempts == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_transient_http_status_retries_then_succeeds(status):
    calls = []
    sleeps = []

    def request_json(url, payload, source_name):
        calls.append(None)
        if len(calls) == 1:
            raise transient_error("transient_http_error", status=status)
        return {"jobPostings": [], "total": 0}

    source = WorkdaySource(
        min_interval_seconds=0,
        request_json=request_json,
        sleeper=sleeps.append,
        jitter=lambda low, high: low,
    )

    assert source.fetch(workday_company()) == []
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_retry_after_is_respected_and_capped():
    calls = []
    sleeps = []

    def request_json(url, payload, source_name):
        calls.append(None)
        if len(calls) == 1:
            raise transient_error(
                "rate_limited",
                status=429,
                metadata={"retry_after_seconds": 999, "body_sha256": "b" * 64},
            )
        return {"jobPostings": [], "total": 0}

    source = WorkdaySource(
        min_interval_seconds=0,
        request_json=request_json,
        sleeper=sleeps.append,
        jitter=lambda low, high: low,
    )
    source.fetch(workday_company())

    assert sleeps == [10.0]


@pytest.mark.parametrize("error", [TimeoutError(), URLError(TimeoutError()), ConnectionResetError()])
def test_network_failures_retry(error):
    calls = []
    sleeps = []

    def request_json(url, payload, source_name):
        calls.append(None)
        if len(calls) == 1:
            def fail(request, timeout):
                raise error

            return post_json_response(url, payload, source_name, opener=fail)
        return post_json_response(
            url,
            payload,
            source_name,
            opener=lambda request, timeout: FakeResponse(b'{"jobPostings": [], "total": 0}'),
        )

    source = WorkdaySource(
        min_interval_seconds=0,
        request_json=request_json,
        sleeper=sleeps.append,
        jitter=lambda low, high: low,
    )
    source.fetch(workday_company())

    assert len(calls) == 2
    assert sleeps == [1.0]


def test_transient_html_followed_by_valid_json_recovers():
    calls = []

    def request_json(url, payload, source_name):
        calls.append(None)
        if len(calls) == 1:
            raise transient_error("html_challenge")
        return {"jobPostings": [], "total": 0}

    source = WorkdaySource(
        min_interval_seconds=0,
        request_json=request_json,
        sleeper=lambda delay: None,
        jitter=lambda low, high: low,
    )

    source.fetch(workday_company())
    assert source.last_diagnostics.request_attempts == 2
    assert source.last_diagnostics.last_transport_error == "html_challenge"


def test_three_transient_failures_raise_one_final_failure_and_two_retries(caplog):
    sleeps = []

    def request_json(url, payload, source_name):
        raise transient_error("html_challenge")

    source = WorkdaySource(
        min_interval_seconds=0,
        request_json=request_json,
        sleeper=sleeps.append,
        jitter=lambda low, high: low,
    )
    with caplog.at_level(logging.WARNING, logger="watcher.sources.workday"):
        with pytest.raises(SourceFetchError) as exc_info:
            source.fetch(workday_company())

    assert exc_info.value.attempt_count == 3
    assert sleeps == [1.0, 3.0]
    assert caplog.text.count("Workday transport retry:") == 2
    assert caplog.text.count("Workday transport failure:") == 1


@pytest.mark.parametrize("status", [400, 404])
def test_nonretryable_http_errors_fail_immediately(status):
    calls = []
    sleeps = []

    def request_json(url, payload, source_name):
        calls.append(None)
        raise SourceFetchError(
            "permanent",
            error_code="permanent_http_error",
            status_code=status,
            retryable=False,
        )

    source = WorkdaySource(
        min_interval_seconds=0,
        request_json=request_json,
        sleeper=sleeps.append,
    )
    with pytest.raises(SourceFetchError):
        source.fetch(workday_company())

    assert len(calls) == 1
    assert sleeps == []


def test_deterministic_schema_error_does_not_retry():
    calls = []
    sleeps = []

    def request_json(url, payload, source_name):
        calls.append(None)
        return {"jobPostings": "not-a-list", "total": 1}

    source = WorkdaySource(
        min_interval_seconds=0,
        request_json=request_json,
        sleeper=sleeps.append,
    )

    with pytest.raises(SourceSchemaError):
        source.fetch(workday_company())

    assert len(calls) == 1
    assert sleeps == []


def test_transport_retry_count_survives_a_later_schema_failure():
    calls = []

    def request_json(url, payload, source_name):
        calls.append(None)
        if len(calls) == 1:
            raise transient_error("html_challenge")
        return {"jobPostings": "not-a-list", "total": 1}

    source = WorkdaySource(
        min_interval_seconds=0,
        request_json=request_json,
        sleeper=lambda delay: None,
        jitter=lambda low, high: low,
    )

    with pytest.raises(SourceSchemaError):
        source.fetch(workday_company())

    assert source.last_diagnostics.request_attempts == 2
    assert source.last_diagnostics.retry_attempts == 1


def test_backoff_jitter_is_injectable_and_deterministic():
    assert workday_retry_delay(1, jitter=lambda low, high: 0.25) == 1.25
    assert workday_retry_delay(2, jitter=lambda low, high: 1.5) == 4.5


def test_pacer_waits_between_tenants_and_can_be_disabled():
    times = iter([0.0, 0.1, 0.5])
    sleeps = []
    pacer = WorkdayPacer(0.5, sleeper=sleeps.append, clock=lambda: next(times))

    assert pacer.wait_for_tenant() == 0
    assert pacer.wait_for_tenant() == pytest.approx(0.4)
    assert sleeps == [pytest.approx(0.4)]

    disabled = WorkdayPacer(0, sleeper=lambda delay: pytest.fail("unexpected sleep"), clock=lambda: 0)
    disabled.wait_for_tenant()
    disabled.wait_for_tenant()


def test_pagination_does_not_apply_tenant_pacing_per_page():
    payloads = [
        {"jobPostings": [{"title": "One", "externalPath": "/job/One"}], "total": 2},
        {"jobPostings": [{"title": "Two", "externalPath": "/job/Two"}], "total": 2},
    ]
    sleeps = []
    source = WorkdaySource(
        min_interval_seconds=0.5,
        request_json=lambda url, payload, source_name: payloads.pop(0),
        sleeper=sleeps.append,
        clock=lambda: 0.0,
    )

    rows = source.fetch(workday_company())

    assert len(rows) == 2
    assert sleeps == []
    assert source.last_diagnostics.request_attempts == 2


def test_consecutive_tenant_fetches_use_adapter_pacing_once():
    times = iter([0.0, 0.1, 0.5])
    sleeps = []
    source = WorkdaySource(
        min_interval_seconds=0.5,
        request_json=lambda url, payload, source_name: {"jobPostings": [], "total": 0},
        sleeper=sleeps.append,
        clock=lambda: next(times),
    )

    source.fetch(workday_company("First"))
    source.fetch(workday_company("Second"))

    assert sleeps == [pytest.approx(0.4)]


def test_pacer_state_is_instance_local():
    first_sleeps = []
    second_sleeps = []
    first = WorkdayPacer(1, sleeper=first_sleeps.append, clock=lambda: 0)
    second = WorkdayPacer(1, sleeper=second_sleeps.append, clock=lambda: 0)

    first.wait_for_tenant()
    second.wait_for_tenant()

    assert first_sleeps == []
    assert second_sleeps == []


@pytest.mark.parametrize("value", ["abc", "-1", "11", "nan", "inf"])
def test_invalid_workday_interval_fails_clearly(value):
    with pytest.raises(ConfigError, match="WATCHER_WORKDAY_MIN_INTERVAL_SECONDS"):
        workday_min_interval_seconds(value)


def test_workday_interval_zero_is_valid():
    assert workday_min_interval_seconds("0") == 0
