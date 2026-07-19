from types import SimpleNamespace

from scripts.probe_workday_transport import DEFAULT_COMPANIES, probe_company
from watcher.config import CompanyCfg
from watcher.sources.base import SourceFetchError


def company():
    return CompanyCfg(
        name="Probe Co",
        ats="workday",
        token="probe",
        workday_shard="wd5",
        workday_site="Careers",
    )


class SuccessfulProbe:
    last_diagnostics = SimpleNamespace(request_attempts=2, retry_attempts=1)

    def probe_transport(self, selected):
        return (
            {"jobPostings": []},
            {
                "status": 200,
                "content_type": "application/json",
                "content_encoding": "",
                "body_kind": "json",
                "body_bytes": 24,
                "body_sha256": "a" * 64,
                "json_decoded": True,
            },
        )


class FailedProbe:
    last_diagnostics = SimpleNamespace(request_attempts=3, retry_attempts=2)

    def probe_transport(self, selected):
        raise SourceFetchError(
            "safe failure",
            error_code="html_challenge",
            retryable=True,
            response_metadata={
                "status": 200,
                "content_type": "text/html",
                "body_kind": "html_challenge",
                "body_bytes": 4000,
                "body_sha256": "b" * 64,
            },
            attempt_count=3,
        )


def test_probe_defaults_are_bounded_to_five_representative_tenants():
    assert DEFAULT_COMPANIES == (
        "Cornerstone Research",
        "Merck",
        "Capital One",
        "Salesforce",
        "Eli Lilly and Company",
    )
    assert len(DEFAULT_COMPANIES) == 5


def test_probe_success_reports_only_safe_transport_metadata():
    result = probe_company(SuccessfulProbe(), company())

    assert result["json_decoded"] is True
    assert result["jobs_field_present"] is True
    assert result["attempt_count"] == 2
    assert result["retries_recovered"] is True
    assert result["body_hash_prefix"] == "a" * 12
    assert "body" not in result
    assert "headers" not in result
    assert "cookies" not in result


def test_probe_failure_reports_classification_without_raw_response():
    result = probe_company(FailedProbe(), company())

    assert result["error_code"] == "html_challenge"
    assert result["body_kind"] == "html_challenge"
    assert result["attempt_count"] == 3
    assert result["json_decoded"] is False
    assert "body" not in result
