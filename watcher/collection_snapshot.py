"""Versioned, validated collection batches for offline watcher replay."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from internship_signal.domain.jobs import CANONICAL_COLUMNS
from watcher.config import WatcherConfig
from watcher.source_health import (
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    SourceAttempt,
    iso_utc,
    utc_datetime,
)

# Increment whenever the persisted snapshot structure changes. Explicitly
# listed older versions receive deterministic compatibility handling; all
# other versions fail before entering the watcher pipeline.
COLLECTION_SNAPSHOT_SCHEMA_VERSION = 3
_COMPATIBLE_COLLECTION_SNAPSHOT_VERSIONS = frozenset({2, 3})
COLLECTION_SNAPSHOT_EXTENSION = ".json.gz"
DEFAULT_COLLECTION_SNAPSHOT_DIR = Path("watcher/collection-snapshots")
_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "captured_at",
        "collection_config_fingerprint",
        "rows",
        "errors",
        "source_attempts",
        "github_feed_counters",
        "workday_transport",
    }
)
_GITHUB_COUNTER_FIELDS = frozenset({"configured", "succeeded"})
_WORKDAY_TRANSPORT_FIELDS = frozenset(
    {
        "attempted",
        "succeeded",
        "failed",
        "request_attempts",
        "retry_attempts",
        "failure_codes",
    }
)
_SOURCE_ATTEMPT_FIELDS_V2 = frozenset(
    {
        "health_key",
        "run_id",
        "observed_at",
        "source_kind",
        "company",
        "adapter",
        "attempted",
        "succeeded",
        "rows_returned",
        "error_kind",
        "error_message",
        "feed_label",
        "unsupported_reason",
    }
)
_SOURCE_ATTEMPT_DIAGNOSTIC_FIELDS = frozenset(
    {
        "malformed_row_count",
        "schema_error_row_count",
        "duplicate_row_count",
        "failed_request_count",
        "incomplete",
        "truncated",
        "reason_codes",
        "degraded",
        "complete",
    }
)
_SOURCE_ATTEMPT_FIELDS = (
    _SOURCE_ATTEMPT_FIELDS_V2 | _SOURCE_ATTEMPT_DIAGNOSTIC_FIELDS
)
_ROW_FIELDS = frozenset((*CANONICAL_COLUMNS, "extra"))


class CollectionSnapshotError(ValueError):
    """A snapshot cannot be safely created, loaded, or replayed."""


@dataclass(frozen=True)
class CollectionBatch:
    """Immutable envelope around one complete collection result.

    Canonical rows and nested JSON values are recursively frozen. The existing
    backend pipeline receives fresh mutable copies through ``mutable_rows()``.
    """

    schema_version: int
    captured_at: datetime
    collection_config_fingerprint: str
    rows: tuple[Mapping[str, object], ...]
    errors: tuple[str, ...]
    source_attempts: tuple[SourceAttempt, ...]
    github_feeds_configured: int = 0
    github_feeds_succeeded: int = 0
    workday_attempted: int = 0
    workday_succeeded: int = 0
    workday_failed: int = 0
    workday_request_attempts: int = 0
    workday_retry_attempts: int = 0
    workday_failure_codes: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.captured_at, datetime):
            raise CollectionSnapshotError(
                "Collection snapshot captured_at must be a datetime"
            )
        if self.captured_at.tzinfo is None:
            raise CollectionSnapshotError(
                "Collection snapshot captured_at must include a timezone"
            )
        object.__setattr__(self, "captured_at", utc_datetime(self.captured_at))
        object.__setattr__(
            self,
            "rows",
            tuple(
                _freeze_json_value(dict(row))
                for row in self.rows
            ),
        )
        object.__setattr__(
            self,
            "errors",
            tuple(str(error) for error in self.errors),
        )
        object.__setattr__(
            self,
            "source_attempts",
            tuple(self.source_attempts),
        )
        object.__setattr__(
            self,
            "workday_failure_codes",
            tuple(
                sorted(
                    (str(code), int(count))
                    for code, count in self.workday_failure_codes
                )
            ),
        )
        _validate_batch(self)

    @classmethod
    def create(
        cls,
        *,
        captured_at: datetime,
        collection_config_fingerprint: str,
        rows: Sequence[Mapping[str, object]],
        errors: Sequence[str],
        source_attempts: Sequence[SourceAttempt],
        github_feeds_configured: int = 0,
        github_feeds_succeeded: int = 0,
        workday_attempted: int = 0,
        workday_succeeded: int = 0,
        workday_failed: int = 0,
        workday_request_attempts: int = 0,
        workday_retry_attempts: int = 0,
        workday_failure_codes: Mapping[str, int] | Sequence[tuple[str, int]] = (),
    ) -> "CollectionBatch":
        failure_items = (
            workday_failure_codes.items()
            if isinstance(workday_failure_codes, Mapping)
            else workday_failure_codes
        )
        batch = cls(
            schema_version=COLLECTION_SNAPSHOT_SCHEMA_VERSION,
            captured_at=captured_at,
            collection_config_fingerprint=str(collection_config_fingerprint),
            rows=tuple(rows),
            errors=tuple(str(error) for error in errors),
            source_attempts=tuple(source_attempts),
            github_feeds_configured=github_feeds_configured,
            github_feeds_succeeded=github_feeds_succeeded,
            workday_attempted=workday_attempted,
            workday_succeeded=workday_succeeded,
            workday_failed=workday_failed,
            workday_request_attempts=workday_request_attempts,
            workday_retry_attempts=workday_retry_attempts,
            workday_failure_codes=tuple(
                sorted((str(code), int(count)) for code, count in failure_items)
            ),
        )
        return batch

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "captured_at": iso_utc(self.captured_at),
            "collection_config_fingerprint": self.collection_config_fingerprint,
            "rows": [
                _mutable_json_value(row)
                for row in self.rows
            ],
            "errors": list(self.errors),
            "source_attempts": [
                _source_attempt_to_dict(attempt)
                for attempt in self.source_attempts
            ],
            "github_feed_counters": {
                "configured": self.github_feeds_configured,
                "succeeded": self.github_feeds_succeeded,
            },
            "workday_transport": {
                "attempted": self.workday_attempted,
                "succeeded": self.workday_succeeded,
                "failed": self.workday_failed,
                "request_attempts": self.workday_request_attempts,
                "retry_attempts": self.workday_retry_attempts,
                "failure_codes": {
                    code: count for code, count in self.workday_failure_codes
                },
            },
        }

    def mutable_rows(self) -> list[dict]:
        """Return isolated dictionaries for the mutating backend deduper."""

        return [
            _mutable_json_value(row)
            for row in self.rows
        ]


def collection_config_fingerprint(config: WatcherConfig) -> str:
    """Hash only settings that can affect collection rows or their order."""

    payload = {
        "companies": [
            {
                "name": company.name,
                "aliases": list(company.aliases),
                "ats": company.ats,
                "token": company.token,
                "workday_shard": company.workday_shard,
                "workday_site": company.workday_site,
                "workday_detail_policy": company.workday_detail_policy,
                "oracle_hcm_host": company.oracle_hcm_host,
                "oracle_hcm_site": company.oracle_hcm_site,
                "talentbrew_host": company.talentbrew_host,
                "talentbrew_site_id": company.talentbrew_site_id,
                "talentbrew_category_id": company.talentbrew_category_id,
                "talentbrew_category_name": company.talentbrew_category_name,
                "icims_variant": company.icims_variant,
                "icims_host": company.icims_host,
                "icims_portals": list(company.icims_portals),
                "successfactors_host": company.successfactors_host,
                "successfactors_site_prefix": company.successfactors_site_prefix,
                "successfactors_locale": company.successfactors_locale,
                "paylocity_company_id": company.paylocity_company_id,
                "paylocity_module_id": company.paylocity_module_id,
                "paylocity_slug": company.paylocity_slug,
                "brassring_host": company.brassring_host,
                "brassring_partner_id": company.brassring_partner_id,
                "brassring_site_id": company.brassring_site_id,
                "taleo_sourcing_host": company.taleo_sourcing_host,
                "taleo_sourcing_site": company.taleo_sourcing_site,
                "ukg_host": company.ukg_host,
                "ukg_tenant": company.ukg_tenant,
                "ukg_board_id": company.ukg_board_id,
                "eightfold_host": company.eightfold_host,
                "eightfold_domain": company.eightfold_domain,
                "eightfold_variant": company.eightfold_variant,
                "module": company.module,
                "terms": list(company.terms),
            }
            for company in config.companies
        ],
        "terms": list(config.terms),
        "github_listing_sources": [
            {
                "name": source.name,
                "format": source.format,
                "url": source.url,
                "default_term": source.default_term,
                "priority": source.priority,
            }
            for source in config.effective_github_listing_sources()
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def save_collection_snapshot(
    batch: CollectionBatch,
    path: str | Path,
) -> None:
    """Atomically save one validated batch as UTF-8 gzip JSON."""

    _validate_batch(batch)
    target = _validated_snapshot_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with temporary.open("wb") as raw_stream:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_stream,
                mtime=0,
            ) as compressed_stream:
                for chunk in encoder.iterencode(batch.as_dict()):
                    compressed_stream.write(chunk.encode("utf-8"))
                compressed_stream.write(b"\n")
            raw_stream.flush()
            os.fsync(raw_stream.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def load_collection_snapshot(path: str | Path) -> CollectionBatch:
    """Load and fully validate a collection snapshot before it is processed."""

    source = _validated_snapshot_path(path)
    try:
        with gzip.open(source, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
    except FileNotFoundError as exc:
        raise CollectionSnapshotError(
            f"Collection snapshot not found: {source}"
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError, EOFError) as exc:
        raise CollectionSnapshotError(
            f"Collection snapshot is corrupt or truncated: {source}"
        ) from exc
    return _batch_from_dict(payload)


def _batch_from_dict(value: object) -> CollectionBatch:
    payload = _require_mapping(value, "snapshot")
    _require_exact_fields(payload, _SNAPSHOT_FIELDS, "snapshot")
    version = _require_int(payload.get("schema_version"), "schema_version")
    if version not in _COMPATIBLE_COLLECTION_SNAPSHOT_VERSIONS:
        raise CollectionSnapshotError(
            "Unsupported collection snapshot schema_version "
            f"{version}; supported versions are 2 and {COLLECTION_SNAPSHOT_SCHEMA_VERSION}"
        )
    captured_text = _require_string(payload.get("captured_at"), "captured_at")
    captured_at = _snapshot_datetime(captured_text, "captured_at")

    rows_value = _require_list(payload.get("rows"), "rows")
    rows = tuple(_validated_row(row, index) for index, row in enumerate(rows_value))
    errors_value = _require_list(payload.get("errors"), "errors")
    errors = tuple(
        _require_string(error, f"errors[{index}]")
        for index, error in enumerate(errors_value)
    )
    attempts_value = _require_list(
        payload.get("source_attempts"),
        "source_attempts",
    )
    attempts = tuple(
        _source_attempt_from_dict(attempt, index, schema_version=version)
        for index, attempt in enumerate(attempts_value)
    )
    github = _require_mapping(
        payload.get("github_feed_counters"),
        "github_feed_counters",
    )
    _require_exact_fields(
        github,
        _GITHUB_COUNTER_FIELDS,
        "github_feed_counters",
    )
    workday = _require_mapping(
        payload.get("workday_transport"),
        "workday_transport",
    )
    _require_exact_fields(
        workday,
        _WORKDAY_TRANSPORT_FIELDS,
        "workday_transport",
    )
    failures = _require_mapping(
        workday.get("failure_codes"),
        "workday_transport.failure_codes",
    )
    return CollectionBatch.create(
        captured_at=captured_at,
        collection_config_fingerprint=_require_fingerprint(
            payload.get("collection_config_fingerprint")
        ),
        rows=rows,
        errors=errors,
        source_attempts=attempts,
        github_feeds_configured=_require_count(
            github.get("configured"),
            "github_feed_counters.configured",
        ),
        github_feeds_succeeded=_require_count(
            github.get("succeeded"),
            "github_feed_counters.succeeded",
        ),
        workday_attempted=_require_count(
            workday.get("attempted"),
            "workday_transport.attempted",
        ),
        workday_succeeded=_require_count(
            workday.get("succeeded"),
            "workday_transport.succeeded",
        ),
        workday_failed=_require_count(
            workday.get("failed"),
            "workday_transport.failed",
        ),
        workday_request_attempts=_require_count(
            workday.get("request_attempts"),
            "workday_transport.request_attempts",
        ),
        workday_retry_attempts=_require_count(
            workday.get("retry_attempts"),
            "workday_transport.retry_attempts",
        ),
        workday_failure_codes={
            _require_string(code, "workday_transport.failure_codes key"): _require_count(
                count,
                f"workday_transport.failure_codes[{code!r}]",
            )
            for code, count in failures.items()
        },
    )


def _validate_batch(batch: CollectionBatch) -> None:
    if batch.schema_version != COLLECTION_SNAPSHOT_SCHEMA_VERSION:
        raise CollectionSnapshotError(
            "Unsupported collection snapshot schema_version "
            f"{batch.schema_version}; supported version is "
            f"{COLLECTION_SNAPSHOT_SCHEMA_VERSION}"
        )
    if batch.captured_at.tzinfo is None:
        raise CollectionSnapshotError("Collection snapshot captured_at must include a timezone")
    _require_fingerprint(batch.collection_config_fingerprint)
    for index, row in enumerate(batch.rows):
        _validated_row(row, index)
    for index, error in enumerate(batch.errors):
        _require_string(error, f"errors[{index}]")
    for index, attempt in enumerate(batch.source_attempts):
        _validate_source_attempt(attempt, index)
    counts = {
        "github_feeds_configured": batch.github_feeds_configured,
        "github_feeds_succeeded": batch.github_feeds_succeeded,
        "workday_attempted": batch.workday_attempted,
        "workday_succeeded": batch.workday_succeeded,
        "workday_failed": batch.workday_failed,
        "workday_request_attempts": batch.workday_request_attempts,
        "workday_retry_attempts": batch.workday_retry_attempts,
    }
    for name, count in counts.items():
        _require_count(count, name)
    # Injected sources used by offline tests and internal tooling are not part
    # of configured_count, so succeeded may legitimately exceed configured.
    if batch.workday_succeeded + batch.workday_failed > batch.workday_attempted:
        raise CollectionSnapshotError(
            "Workday succeeded and failed counts cannot exceed attempted count"
        )
    if batch.workday_request_attempts < batch.workday_attempted:
        raise CollectionSnapshotError(
            "Workday request-attempt count cannot be lower than attempted tenants"
        )
    if batch.workday_retry_attempts > batch.workday_request_attempts:
        raise CollectionSnapshotError(
            "Workday retry count cannot exceed request-attempt count"
        )
    for code, count in batch.workday_failure_codes:
        _require_string(code, "workday_failure_codes key")
        _require_count(count, f"workday_failure_codes[{code!r}]")
    try:
        json.dumps(batch.as_dict(), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CollectionSnapshotError(
            "Collection snapshot contains a non-JSON value"
        ) from exc


def _validated_row(value: object, index: int) -> dict:
    row = dict(_require_mapping(value, f"rows[{index}]"))
    _require_exact_fields(row, _ROW_FIELDS, f"rows[{index}]")
    for column in CANONICAL_COLUMNS:
        if not isinstance(row[column], str):
            raise CollectionSnapshotError(
                f"rows[{index}].{column} must be a string"
            )
    if not isinstance(row.get("extra"), Mapping):
        raise CollectionSnapshotError(f"rows[{index}].extra must be an object")
    return row


def _source_attempt_to_dict(attempt: SourceAttempt) -> dict[str, object]:
    return {
        "health_key": attempt.health_key,
        "run_id": attempt.run_id,
        "observed_at": iso_utc(attempt.observed_at),
        "source_kind": attempt.source_kind,
        "company": attempt.company,
        "adapter": attempt.adapter,
        "attempted": attempt.attempted,
        "succeeded": attempt.succeeded,
        "rows_returned": attempt.rows_returned,
        "error_kind": attempt.error_kind,
        "error_message": attempt.error_message,
        "feed_label": attempt.feed_label,
        "unsupported_reason": attempt.unsupported_reason,
        "malformed_row_count": attempt.malformed_row_count,
        "schema_error_row_count": attempt.schema_error_row_count,
        "duplicate_row_count": attempt.duplicate_row_count,
        "failed_request_count": attempt.failed_request_count,
        "incomplete": attempt.incomplete,
        "truncated": attempt.truncated,
        "reason_codes": list(attempt.reason_codes),
        "degraded": attempt.degraded,
        "complete": attempt.complete,
    }


def _source_attempt_from_dict(
    value: object,
    index: int,
    *,
    schema_version: int,
) -> SourceAttempt:
    item = _require_mapping(value, f"source_attempts[{index}]")
    _require_exact_fields(
        item,
        _SOURCE_ATTEMPT_FIELDS if schema_version >= 3 else _SOURCE_ATTEMPT_FIELDS_V2,
        f"source_attempts[{index}]",
    )
    observed_text = _require_string(
        item.get("observed_at"),
        f"source_attempts[{index}].observed_at",
    )
    observed_at = _snapshot_datetime(
        observed_text,
        f"source_attempts[{index}].observed_at",
    )
    succeeded = item.get("succeeded")
    if succeeded is not None and not isinstance(succeeded, bool):
        raise CollectionSnapshotError(
            f"source_attempts[{index}].succeeded must be boolean or null"
        )
    rows_returned = item.get("rows_returned")
    if rows_returned is not None:
        rows_returned = _require_count(
            rows_returned,
            f"source_attempts[{index}].rows_returned",
        )
    attempt = SourceAttempt(
        health_key=_require_string(
            item.get("health_key"),
            f"source_attempts[{index}].health_key",
        ),
        run_id=_require_string(
            item.get("run_id"),
            f"source_attempts[{index}].run_id",
        ),
        observed_at=observed_at,
        source_kind=_require_string(
            item.get("source_kind"),
            f"source_attempts[{index}].source_kind",
        ),
        company=_optional_string(
            item.get("company"),
            f"source_attempts[{index}].company",
        ),
        adapter=_require_string(
            item.get("adapter"),
            f"source_attempts[{index}].adapter",
        ),
        attempted=_require_bool(
            item.get("attempted"),
            f"source_attempts[{index}].attempted",
        ),
        succeeded=succeeded,
        rows_returned=rows_returned,
        error_kind=_optional_string(
            item.get("error_kind"),
            f"source_attempts[{index}].error_kind",
        ),
        error_message=_optional_string(
            item.get("error_message"),
            f"source_attempts[{index}].error_message",
        ),
        feed_label=_optional_string(
            item.get("feed_label"),
            f"source_attempts[{index}].feed_label",
        ),
        unsupported_reason=_optional_string(
            item.get("unsupported_reason"),
            f"source_attempts[{index}].unsupported_reason",
        ),
        malformed_row_count=_optional_count_field(
            item, "malformed_row_count", index
        ),
        schema_error_row_count=_optional_count_field(
            item, "schema_error_row_count", index
        ),
        duplicate_row_count=_optional_count_field(
            item, "duplicate_row_count", index
        ),
        failed_request_count=_optional_count_field(
            item, "failed_request_count", index
        ),
        incomplete=_optional_bool_field(item, "incomplete", index),
        truncated=_optional_bool_field(item, "truncated", index),
        reason_codes=_reason_codes_field(item, index),
        degraded=_optional_bool_field(item, "degraded", index),
        complete=_optional_bool_field(item, "complete", index),
    )
    _validate_source_attempt(attempt, index)
    return attempt


def _validate_source_attempt(attempt: SourceAttempt, index: int) -> None:
    if attempt.source_kind not in {SOURCE_KIND_DIRECT, SOURCE_KIND_GITHUB_FEED}:
        raise CollectionSnapshotError(
            f"source_attempts[{index}].source_kind is unsupported"
        )
    if not isinstance(attempt.attempted, bool):
        raise CollectionSnapshotError(
            f"source_attempts[{index}].attempted must be boolean"
        )
    if attempt.succeeded is not None and not isinstance(attempt.succeeded, bool):
        raise CollectionSnapshotError(
            f"source_attempts[{index}].succeeded must be boolean or null"
        )
    if attempt.rows_returned is not None:
        _require_count(
            attempt.rows_returned,
            f"source_attempts[{index}].rows_returned",
        )
    if attempt.observed_at.tzinfo is None:
        raise CollectionSnapshotError(
            f"source_attempts[{index}].observed_at must include a timezone"
        )
    if attempt.attempted:
        if attempt.succeeded is None:
            raise CollectionSnapshotError(
                f"source_attempts[{index}].succeeded must be boolean when attempted"
            )
        if attempt.succeeded and attempt.rows_returned is None:
            raise CollectionSnapshotError(
                f"source_attempts[{index}].rows_returned is required on success"
            )
    elif attempt.succeeded is not None or attempt.rows_returned is not None:
        raise CollectionSnapshotError(
            f"source_attempts[{index}] cannot have an outcome when not attempted"
        )
    for field in (
        "malformed_row_count",
        "schema_error_row_count",
        "duplicate_row_count",
        "failed_request_count",
    ):
        value = getattr(attempt, field)
        if value is not None:
            _require_count(value, f"source_attempts[{index}].{field}")
    for field in ("incomplete", "truncated", "degraded", "complete"):
        value = getattr(attempt, field)
        if value is not None and not isinstance(value, bool):
            raise CollectionSnapshotError(
                f"source_attempts[{index}].{field} must be boolean or null"
            )
    if len(attempt.reason_codes) > 12 or any(
        not isinstance(code, str) or not code or len(code) > 80
        for code in attempt.reason_codes
    ):
        raise CollectionSnapshotError(
            f"source_attempts[{index}].reason_codes is invalid"
        )


def _optional_count_field(
    item: Mapping,
    name: str,
    index: int,
) -> int | None:
    value = item.get(name)
    if value is None:
        return None
    return _require_count(value, f"source_attempts[{index}].{name}")


def _optional_bool_field(
    item: Mapping,
    name: str,
    index: int,
) -> bool | None:
    value = item.get(name)
    if value is None:
        return None
    return _require_bool(value, f"source_attempts[{index}].{name}")


def _reason_codes_field(item: Mapping, index: int) -> tuple[str, ...]:
    value = item.get("reason_codes")
    if value is None:
        return ()
    codes = _require_list(value, f"source_attempts[{index}].reason_codes")
    result = tuple(
        _require_string(code, f"source_attempts[{index}].reason_codes")
        for code in codes
    )
    if len(result) > 12 or any(not code or len(code) > 80 for code in result):
        raise CollectionSnapshotError(
            f"source_attempts[{index}].reason_codes is invalid"
        )
    return result


def _validated_snapshot_path(path: str | Path) -> Path:
    result = Path(path)
    if not str(result).endswith(COLLECTION_SNAPSHOT_EXTENSION):
        raise CollectionSnapshotError(
            "Collection snapshot paths must end with "
            f"{COLLECTION_SNAPSHOT_EXTENSION}"
        )
    return result


def _require_mapping(value: object, field: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise CollectionSnapshotError(f"Collection snapshot {field} must be an object")
    return value


def _require_exact_fields(
    value: Mapping,
    expected: frozenset[str],
    field: str,
) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unexpected = sorted(
        (actual - expected),
        key=lambda item: str(item),
    )
    problems = []
    if missing:
        problems.append(f"missing fields: {', '.join(missing)}")
    if unexpected:
        problems.append(
            "unexpected fields: "
            + ", ".join(str(item) for item in unexpected)
        )
    if problems:
        raise CollectionSnapshotError(
            f"Collection snapshot {field} has {'; '.join(problems)}"
        )


def _require_list(value: object, field: str) -> list:
    if not isinstance(value, list):
        raise CollectionSnapshotError(f"Collection snapshot {field} must be an array")
    return value


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise CollectionSnapshotError(f"Collection snapshot {field} must be a string")
    return value


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, field)


def _require_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise CollectionSnapshotError(f"Collection snapshot {field} must be boolean")
    return value


def _require_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CollectionSnapshotError(f"Collection snapshot {field} must be an integer")
    return value


def _require_count(value: object, field: str) -> int:
    result = _require_int(value, field)
    if result < 0 or result > 1_000_000_000:
        raise CollectionSnapshotError(
            f"Collection snapshot {field} must be between 0 and 1000000000"
        )
    return result


def _require_fingerprint(value: object) -> str:
    fingerprint = _require_string(
        value,
        "collection_config_fingerprint",
    )
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint
    ):
        raise CollectionSnapshotError(
            "Collection snapshot collection_config_fingerprint must be a "
            "lowercase SHA-256 digest"
        )
    return fingerprint


def _snapshot_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectionSnapshotError(
            f"Collection snapshot {field} must be a valid ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise CollectionSnapshotError(
            f"Collection snapshot {field} must include a timezone"
        )
    return utc_datetime(parsed)


def _freeze_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise CollectionSnapshotError(
                "Collection snapshot object keys must be strings"
            )
        return MappingProxyType(
            {
                key: _freeze_json_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _mutable_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _mutable_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_mutable_json_value(item) for item in value]
    return value
