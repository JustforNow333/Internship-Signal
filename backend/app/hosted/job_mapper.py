"""Convert final watcher jobs into bounded hosted persistence values."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from backend.app.normalize import parse_date
from watcher.filters import is_internship, is_open

from .catalog import CompanyCatalog

MAX_POSTING_TEXT_LENGTH = 500_000
MAX_SOURCE_METADATA_BYTES = 4_096

_DIRECT_ADAPTERS = frozenset(
    {
        "ashby",
        "greenhouse",
        "lever",
        "smartrecruiters",
        "workable",
        "workday",
    }
)
_BACKSTOP_ADAPTERS = frozenset(
    {"github_listings", "github_markdown_table"}
)
_PUBLIC_ADAPTERS = _DIRECT_ADAPTERS | _BACKSTOP_ADAPTERS

# This is the single watcher-classification to hosted-role conversion table.
WATCHER_ROLE_TRACK_TO_HOSTED_ROLE = {
    "backend": "software_engineering",
    "full_stack": "software_engineering",
    "frontend": "software_engineering",
    "general_swe": "software_engineering",
    "platform_infra": "software_engineering",
    "devops": "software_engineering",
    "cloud": "software_engineering",
    "sdet_qa_automation": "software_engineering",
    "ml_ai": "machine_learning_ai",
    "data_science": "data_science",
    "data_engineering": "data_engineering",
    "quant_dev": "quantitative_development",
    "technical_product": "product_management",
    "product": "product_management",
    "embedded_software": "hardware_embedded",
    "firmware": "hardware_embedded",
    "electrical_hardware": "hardware_embedded",
}
_OTHER_ENGINEERING_TRACKS = frozenset(
    {
        "customer_experience",
        "it_support",
        "solutions_engineering",
        "mechanical_manufacturing",
        "civil_structural",
        "quality_test",
        "factory_automation",
        "other_engineering",
    }
)
_WATCHER_ROLE_FALLBACKS = {
    "swe": "software_engineering",
    "ml_ai": "machine_learning_ai",
    "data_science": "data_science",
    "quant": "quantitative_development",
    "product": "product_management",
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MULTILINE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PUBLIC_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_NO_DATE_RE = re.compile(r"rolling|open until filled|ongoing", re.IGNORECASE)
_RELATIVE_POSTING_DATE_RE = re.compile(
    r"(?:posted\s+)?(?:today|yesterday|\d+\+?\s+days?\s+ago)",
    re.IGNORECASE,
)
FINAL_JOBS_STRUCTURE_REASONS = frozenset(
    {
        "final_jobs_not_sequence",
        "final_job_not_object",
        "duplicate_watcher_job_id",
    }
)


class FinalJobsStructureError(ValueError):
    """The analyzed collection is not a valid final-job sequence."""

    def __init__(self, reason: str) -> None:
        safe_reason = (
            reason
            if reason in FINAL_JOBS_STRUCTURE_REASONS
            else "invalid_structure"
        )
        super().__init__(safe_reason)
        self.reason = safe_reason


class _SkipJob(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class MappedJob:
    watcher_job_id: str
    company_id: str
    company_name: str
    title: str
    location: str
    remote_status: str
    role_id: str
    description: str
    requirements: str
    application_url: str | None
    posting_date: date | None
    deadline: date | None
    is_open: bool
    source_metadata: dict[str, object]

    def business_values(self) -> dict[str, object]:
        return {
            "company_id": self.company_id,
            "company_name": self.company_name,
            "title": self.title,
            "location": self.location,
            "remote_status": self.remote_status,
            "role_id": self.role_id,
            "description": self.description,
            "requirements": self.requirements,
            "application_url": self.application_url,
            "posting_date": self.posting_date,
            "deadline": self.deadline,
            "is_open": self.is_open,
            "source_metadata": self.source_metadata,
        }


@dataclass(frozen=True)
class MappedJobs:
    jobs: tuple[MappedJob, ...]
    jobs_received: int
    skipped_reasons: dict[str, int]

    @property
    def jobs_skipped(self) -> int:
        return sum(self.skipped_reasons.values())


def map_final_jobs(
    final_jobs: Sequence[Mapping[str, object]],
    catalog: CompanyCatalog,
) -> MappedJobs:
    if isinstance(final_jobs, (str, bytes)) or not isinstance(final_jobs, Sequence):
        raise FinalJobsStructureError("final_jobs_not_sequence")

    mapped: list[MappedJob] = []
    skipped: Counter[str] = Counter()
    watcher_ids: set[str] = set()
    for value in final_jobs:
        if not isinstance(value, Mapping):
            raise FinalJobsStructureError("final_job_not_object")
        try:
            job = _map_final_job(value, catalog)
        except _SkipJob as exc:
            skipped[exc.reason] += 1
            continue
        if job.watcher_job_id in watcher_ids:
            raise FinalJobsStructureError("duplicate_watcher_job_id")
        watcher_ids.add(job.watcher_job_id)
        mapped.append(job)
    return MappedJobs(
        jobs=tuple(mapped),
        jobs_received=len(final_jobs),
        skipped_reasons=dict(sorted(skipped.items())),
    )


def _map_final_job(
    job: Mapping[str, object],
    catalog: CompanyCatalog,
) -> MappedJob:
    watcher_job_id = _bounded_text(
        job.get("id"),
        maximum=128,
        required=True,
        reason="invalid_watcher_job_id",
    )
    company_value = _bounded_text(
        job.get("company"),
        maximum=200,
        required=True,
        reason="unsupported_company",
    )
    company = catalog.resolve(company_value)
    if company is None or not company.selectable:
        raise _SkipJob("unsupported_company")
    title = _bounded_text(
        job.get("title"),
        maximum=500,
        required=True,
        reason="invalid_title",
    )
    location = _bounded_text(
        job.get("location"),
        maximum=500,
        reason="invalid_location",
    )
    remote_status = _bounded_text(
        job.get("remote_status"),
        maximum=120,
        reason="invalid_remote_status",
    )
    description = _bounded_text(
        job.get("description"),
        maximum=MAX_POSTING_TEXT_LENGTH,
        reason="invalid_description",
        allow_multiline=True,
    )
    requirements = _bounded_text(
        job.get("requirements"),
        maximum=MAX_POSTING_TEXT_LENGTH,
        reason="invalid_requirements",
        allow_multiline=True,
    )
    application_url = _application_url(job.get("source_url"))
    posting_date = _optional_date(
        job.get("date_posted"),
        "invalid_posting_date",
        allow_relative=True,
    )
    deadline = _optional_date(job.get("deadline"), "invalid_deadline")
    role_id = _hosted_role_id(job)
    open_status = _open_status(job)
    source_metadata = safe_source_metadata(job.get("extra"))
    return MappedJob(
        watcher_job_id=watcher_job_id,
        company_id=company.id,
        company_name=company.name,
        title=title,
        location=location,
        remote_status=remote_status,
        role_id=role_id,
        description=description,
        requirements=requirements,
        application_url=application_url,
        posting_date=posting_date,
        deadline=deadline,
        is_open=open_status,
        source_metadata=source_metadata,
    )


def _bounded_text(
    value: object,
    *,
    maximum: int,
    reason: str,
    required: bool = False,
    allow_multiline: bool = False,
) -> str:
    if value is None:
        result = ""
    elif isinstance(value, str):
        result = value.strip()
    else:
        raise _SkipJob(reason)
    control_pattern = _MULTILINE_CONTROL_RE if allow_multiline else _CONTROL_RE
    if (
        (required and not result)
        or len(result) > maximum
        or control_pattern.search(result)
    ):
        raise _SkipJob(reason)
    return result


def _application_url(value: object) -> str | None:
    url = _bounded_text(
        value,
        maximum=2048,
        reason="invalid_application_url",
    )
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        raise _SkipJob("invalid_application_url") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise _SkipJob("invalid_application_url")
    return url


def _optional_date(
    value: object,
    reason: str,
    *,
    allow_relative: bool = False,
) -> date | None:
    text = _bounded_text(value, maximum=80, reason=reason)
    if (
        not text
        or _NO_DATE_RE.search(text)
        or (allow_relative and _RELATIVE_POSTING_DATE_RE.fullmatch(text))
    ):
        return None
    parsed = parse_date(text)
    if parsed is None:
        raise _SkipJob(reason)
    return parsed


def _hosted_role_id(job: Mapping[str, object]) -> str:
    classification = job.get("role_classification")
    if not isinstance(classification, Mapping):
        raise _SkipJob("invalid_role")
    track = str(classification.get("role_track") or "").strip()
    direct = WATCHER_ROLE_TRACK_TO_HOSTED_ROLE.get(track)
    if direct is not None:
        return direct
    role = str(classification.get("role") or "").strip()
    if track in _OTHER_ENGINEERING_TRACKS:
        if is_internship(dict(job)):
            return "other_engineering"
        raise _SkipJob("invalid_role")
    fallback = _WATCHER_ROLE_FALLBACKS.get(role)
    if fallback is None:
        raise _SkipJob("invalid_role")
    return fallback


def _open_status(job: Mapping[str, object]) -> bool:
    extra = job.get("extra")
    if not isinstance(extra, Mapping):
        raise _SkipJob("invalid_open_status")
    if "active" in extra and not isinstance(extra["active"], bool):
        raise _SkipJob("invalid_open_status")
    days_left = job.get("deadline_days_left")
    if days_left is not None and (
        isinstance(days_left, bool) or not isinstance(days_left, int)
    ):
        raise _SkipJob("invalid_open_status")
    result = is_open(dict(job))
    if not isinstance(result, bool):
        raise _SkipJob("invalid_open_status")
    return result


def safe_source_metadata(value: object) -> dict[str, object]:
    """Return only a small allowlisted subset of watcher provenance."""

    if not isinstance(value, Mapping):
        raise _SkipJob("invalid_provenance")
    result: dict[str, object] = {}
    source = value.get("source")
    if source == "direct":
        result["source_type"] = "direct"
    elif source == "github":
        result["source_type"] = "backstop"

    adapter = _public_adapter(value.get("source_adapter"))
    if adapter:
        result["adapter"] = adapter

    requisition = value.get("source_requisition_id")
    if requisition is None and source == "direct":
        requisition = value.get("source_id")
    if isinstance(requisition, (str, int)):
        public_id = str(requisition).strip()
        if _PUBLIC_ID_RE.fullmatch(public_id):
            result["requisition_id"] = public_id

    adapters = {adapter} if adapter else set()
    details = value.get("source_details")
    if isinstance(details, Mapping):
        for detail in details.values():
            if not isinstance(detail, Mapping):
                continue
            merged_adapter = _public_adapter(detail.get("source_adapter"))
            if merged_adapter:
                adapters.add(merged_adapter)
    if len(adapters) > 1:
        result["merged_adapters"] = sorted(adapters)[:8]

    encoded = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_SOURCE_METADATA_BYTES:
        raise _SkipJob("invalid_provenance")
    return result


def _public_adapter(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized if normalized in _PUBLIC_ADAPTERS else None
