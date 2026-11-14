"""Chunked hosted analysis-cache lookups and stores.

``ChunkedAnalysisCache`` only splits work across the deployed
``watcher.analysis_cache.AnalysisCache`` methods, so each committed
transaction (and its rollback journal) stays bounded. Every cache file here is
written by the deployed cache code, and every chunked result is compared with
the deployed single-transaction result on an identical copy.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.hosted import analysis_cache as hosted_cache
from app.hosted import chunked_cache
from app.hosted.chunked_cache import ChunkedAnalysisCache, ChunkStats
from app.hosted.timing import HostedTiming

from backend.app import config as backend_config
from backend.app.ingest import analyze_rows, analyze_static_row, deduplicate_rows
from backend.app.profile import load_profile
from watcher.analysis_cache import AnalysisCache, CacheLookupResult
from watcher.source_health import iso_utc
from watcher.sources.base import make_row

VERSION = 7_000_000_000_000_011
OTHER_VERSION = 7_000_000_000_000_012
WRITTEN = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
LOOKUP = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
LINE_RE = re.compile(r"^HOSTED-TIMING kind=[a-z]+( [a-z_]+=[a-z0-9_.]+)+$")
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv(chunked_cache.CHUNK_ENV, raising=False)
    monkeypatch.delenv(hosted_cache.CACHE_PATH_ENV, raising=False)


@pytest.fixture(scope="module")
def pool() -> list[dict]:
    rows = [
        make_row(
            source="direct",
            source_adapter="workday",
            company="Example Co",
            title=title,
            location="New York, NY",
            description="Build Python services. " * 40 + str(index),
            requirements="Python and SQL. Pursuing a bachelor's degree.",
            source_url=f"https://careers.example.test/jobs/{index}",
            date_posted="2026-10-01",
            internship_type="Summer 2027 Internship",
        )
        for index, title in enumerate(
            ["Software Engineering Intern", "Data Science Intern", "Account Executive"]
        )
    ]
    unique, _ = deduplicate_rows(rows)
    profile, known = load_profile(), backend_config.load_known_companies()
    return [analyze_static_row(row, profile=profile, known=known) for row in unique]


def fp(index: int, version: int = VERSION) -> str:
    return hashlib.sha256(f"{version}:{index}".encode()).hexdigest()


def build(path: Path, pool, count: int = 40) -> Path:
    """A cache written by the deployed code, plus rows the lookups must skip."""

    with AnalysisCache(path, cache_version=VERSION) as cache:
        assert cache.store_many(
            {fp(i): pool[i % len(pool)] for i in range(count)}, accessed_at=WRITTEN
        ) == count
    with AnalysisCache(path, cache_version=OTHER_VERSION) as cache:
        cache.store_many({fp(i, OTHER_VERSION): pool[0] for i in range(5)}, accessed_at=WRITTEN)
    with sqlite3.connect(path) as connection:
        # One undecodable and one structurally invalid current-version row.
        connection.execute(
            "update analysis_cache set artifact_json = '{' where fingerprint = ?", (fp(3),)
        )
        connection.execute(
            "update analysis_cache set artifact_json = '{}' where fingerprint = ?", (fp(4),)
        )
    return path


def twin(path: Path, name: str) -> Path:
    copy_path = path.with_name(name)
    shutil.copyfile(path, copy_path)
    return copy_path


def dump(path: Path) -> list[tuple]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return connection.execute(
            "select fingerprint, cache_version, artifact_json, created_at, last_accessed_at "
            "from analysis_cache order by fingerprint"
        ).fetchall()


def accessed(path: Path) -> dict[str, str]:
    return {row[0]: row[4] for row in dump(path)}


def integrity(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("pragma integrity_check").fetchone()[0]


def deployed_lookup(path: Path, keys, when=LOOKUP) -> CacheLookupResult:
    with AnalysisCache(path, cache_version=VERSION) as cache:
        return cache.lookup_many(keys, accessed_at=when)


def chunked_lookup(path: Path, keys, chunk_rows, when=LOOKUP, **kwargs) -> CacheLookupResult:
    with ChunkedAnalysisCache(
        path, cache_version=VERSION, chunk_rows=chunk_rows, **kwargs
    ) as cache:
        return cache.lookup_many(keys, accessed_at=when)


def lookup_keys() -> list[str]:
    # Hits, a duplicate, misses, other-version keys, and the two bad rows.
    keys = [fp(i) for i in range(40)]
    return [*keys[:10], fp(1), "missing-a", *keys[10:], fp(0, OTHER_VERSION), "missing-b"]


# --- setting -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (10_000, "")),
        ("", (10_000, "")),
        ("  ", (10_000, "")),
        ("2500", (2_500, "")),
        (" 1 ", (1, "")),
        ("0", (0, "")),
        ("off", (0, "")),
        ("OFF", (0, "")),
        ("-5", (10_000, "invalid_chunk_rows")),
        ("1e4", (10_000, "invalid_chunk_rows")),
        ("ten", (10_000, "invalid_chunk_rows")),
    ],
)
def test_chunk_setting_defaults_to_ten_thousand_and_zero_disables(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(chunked_cache.CHUNK_ENV, raising=False)
    else:
        monkeypatch.setenv(chunked_cache.CHUNK_ENV, raw)
    assert chunked_cache.chunk_rows_setting() == expected
    assert chunked_cache.DEFAULT_CHUNK_ROWS == 10_000


# --- lookup equivalence ------------------------------------------------------


@pytest.mark.parametrize("chunk_rows", [1, 7, 10, 41, 10_000])
def test_full_and_partial_hits_match_the_deployed_lookup_exactly(tmp_path, pool, chunk_rows):
    base = build(tmp_path / "cache.sqlite", pool)
    deployed_path, chunked_path = twin(base, "deployed.sqlite"), twin(base, "chunked.sqlite")
    keys = lookup_keys()

    expected = deployed_lookup(deployed_path, keys)
    stats = ChunkStats(chunk_rows=chunk_rows)
    result = chunked_lookup(chunked_path, keys, chunk_rows, stats=stats)

    assert result == expected
    assert (len(result.artifacts), result.invalid, result.failed) == (38, 2, False)
    # Same artifacts, same timestamps, nothing else touched.
    assert dump(chunked_path) == dump(deployed_path)
    unique = len(dict.fromkeys(keys))
    assert stats.lookup_chunks == -(-unique // chunk_rows)
    assert stats.failure == "none"
    assert integrity(chunked_path) == "ok"


def test_full_hit_set_round_trips_every_artifact(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    keys = [fp(i) for i in range(40) if i not in (3, 4)]
    result = chunked_lookup(path, keys, 6)
    assert set(result.artifacts) == set(keys)
    for index, key in zip((i for i in range(40) if i not in (3, 4)), keys):
        assert result.artifacts[key] == json.loads(json.dumps(pool[index % len(pool)]))


def test_empty_and_single_chunk_lookups_delegate_unchanged(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    assert chunked_lookup(path, [], 5) == CacheLookupResult({})
    twin_path = twin(path, "deployed.sqlite")
    keys = [fp(1), fp(2)]
    assert chunked_lookup(path, keys, 5) == deployed_lookup(twin_path, keys)
    assert dump(path) == dump(twin_path)


def test_each_chunk_commits_before_the_next_starts(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    keys = [fp(i) for i in range(40) if i not in (3, 4)]
    observed = []

    def after_chunk(kind, index, cache):
        # Nothing is left open between chunks, and another connection already
        # sees this chunk's committed timestamps.
        assert kind == "lookup"
        assert not cache._conn.in_transaction
        stamped = [k for k, v in accessed(path).items() if v == iso_utc(LOOKUP)]
        observed.append(len(stamped))
        assert not Path(f"{path}-journal").exists()

    chunked_lookup(path, keys, 10, after_chunk=after_chunk)
    assert observed == [10, 20, 30, 38]


def test_one_timestamp_is_used_for_every_chunk_when_none_is_given(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    keys = [fp(i) for i in range(40) if i not in (3, 4)]
    with ChunkedAnalysisCache(path, cache_version=VERSION, chunk_rows=3) as cache:
        cache.lookup_many(keys)
    stamps = {accessed(path)[key] for key in keys}
    assert len(stamps) == 1 and stamps != {iso_utc(WRITTEN)}


# --- lookup failure, interruption, retry -------------------------------------


def make_read_only_after(kind_wanted: str, chunks: int):
    def after_chunk(kind, index, cache):
        if kind == kind_wanted and index == chunks:
            cache._conn.execute("pragma query_only = 1")

    return after_chunk


def test_a_failing_chunk_reports_the_whole_lookup_as_failed(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    before = dump(path)
    keys = [fp(i) for i in range(40)]
    stats = ChunkStats(chunk_rows=10)

    result = chunked_lookup(
        path, keys, 10, stats=stats, after_chunk=make_read_only_after("lookup", 2)
    )

    # Deployed semantics: any lookup error means no hits at all, never a
    # partial set, so the run re-analyzes every row from scratch.
    assert result == CacheLookupResult({}, failed=True)
    assert (stats.failure, stats.lookup_chunks) == ("lookup", 3)
    # Earlier chunks keep their newer timestamps (harmless); every artifact,
    # version and creation time is untouched; the failed chunk rolled back.
    after = dump(path)
    assert [row[:4] for row in after] == [row[:4] for row in before]
    changed = {row[0] for row, old in zip(after, before) if row[4] != old[4]}
    assert changed == {fp(i) for i in range(20) if i not in (3, 4)}
    assert integrity(path) == "ok"

    # Retry: the next run's lookup is complete and identical to deployed.
    reference = twin(build(tmp_path / "reference.sqlite", pool), "ref.sqlite")
    later = LOOKUP + timedelta(hours=2)
    assert chunked_lookup(path, keys, 10, when=later) == deployed_lookup(reference, keys, later)


CRASH_SCRIPT = r"""
import os, sys
sys.path[:0] = [sys.argv[1], os.path.join(sys.argv[1], "backend")]
from datetime import datetime, timezone
from app.hosted.chunked_cache import ChunkedAnalysisCache
path, version, keys = sys.argv[2], int(sys.argv[3]), sys.argv[4].split(",")
cache = ChunkedAnalysisCache(path, cache_version=version, chunk_rows=10)
# A one-page cache forces SQLite to write changed pages into the database
# file mid-transaction, so the crash leaves a genuinely hot journal.
cache._conn.execute("pragma cache_size = 1")
updates = 0
def trace(sql):
    global updates
    if sql.lstrip().lower().startswith("update"):
        updates += 1
        if updates == 25:  # halfway through the third chunk
            os._exit(9)
cache._conn.set_trace_callback(trace)
cache.lookup_many(keys, accessed_at=datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc))
os._exit(0)
"""


def test_a_crash_mid_chunk_is_rolled_back_and_the_retry_is_complete(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    before = dump(path)
    keys = [fp(i) for i in range(40) if i not in (3, 4)]
    completed = subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT, str(REPO), str(path), str(VERSION), ",".join(keys)],
        check=False,
        cwd=REPO,
    )
    assert completed.returncode == 9
    journal = Path(f"{path}-journal")
    assert journal.exists() and journal.stat().st_size > 0

    # The next opener rolls the interrupted chunk back; the two committed
    # chunks keep their newer timestamps and every artifact is intact.
    result = chunked_lookup(path, keys, 10, when=LOOKUP + timedelta(hours=2))
    assert not journal.exists()
    assert integrity(path) == "ok"
    assert set(result.artifacts) == set(keys) and not result.failed
    assert [row[:4] for row in dump(path)] == [row[:4] for row in before]


def test_crash_recovery_leaves_exactly_the_committed_chunks_stamped(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    keys = [fp(i) for i in range(40) if i not in (3, 4)]
    completed = subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT, str(REPO), str(path), str(VERSION), ",".join(keys)],
        check=False,
        cwd=REPO,
    )
    assert completed.returncode == 9
    with AnalysisCache(path, cache_version=VERSION):
        pass  # any read-write open recovers the hot journal
    stamps = accessed(path)
    assert [k for k in keys if stamps[k] == iso_utc(LOOKUP)] == keys[:20]
    assert all(stamps[k] == iso_utc(WRITTEN) for k in keys[20:])
    assert integrity(path) == "ok"


# --- store -------------------------------------------------------------------


def store_batch(pool) -> dict[str, dict]:
    # New rows, upserts over existing rows, and invalid artifacts.
    artifacts = {fp(i): pool[(i + 1) % len(pool)] for i in range(30, 55)}
    artifacts["bad-shape"] = {}
    artifacts["bad-json"] = {**pool[0], "unencodable": float("nan")}
    return artifacts


@pytest.mark.parametrize("chunk_rows", [1, 4, 27, 10_000])
def test_chunked_store_writes_exactly_what_the_deployed_store_writes(tmp_path, pool, chunk_rows):
    base = build(tmp_path / "cache.sqlite", pool)
    deployed_path, chunked_path = twin(base, "deployed.sqlite"), twin(base, "chunked.sqlite")
    artifacts = store_batch(pool)

    with AnalysisCache(deployed_path, cache_version=VERSION) as cache:
        expected = cache.store_many(copy.deepcopy(artifacts), accessed_at=LOOKUP)
    stats = ChunkStats(chunk_rows=chunk_rows)
    with ChunkedAnalysisCache(
        chunked_path, cache_version=VERSION, chunk_rows=chunk_rows, stats=stats
    ) as cache:
        written = cache.store_many(copy.deepcopy(artifacts), accessed_at=LOOKUP)

    assert written == expected == 25
    assert dump(chunked_path) == dump(deployed_path)
    assert stats.store_chunks == -(-len(artifacts) // chunk_rows)
    assert integrity(chunked_path) == "ok"
    with ChunkedAnalysisCache(chunked_path, cache_version=VERSION, chunk_rows=chunk_rows) as cache:
        assert cache.store_many({}, accessed_at=LOOKUP) == 0


def test_a_failing_store_chunk_keeps_earlier_chunks_and_the_retry_completes(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    reference = twin(path, "reference.sqlite")
    artifacts = {fp(i): pool[(i + 1) % len(pool)] for i in range(40, 70)}
    stats = ChunkStats(chunk_rows=10)

    with ChunkedAnalysisCache(
        path, cache_version=VERSION, chunk_rows=10, stats=stats,
        after_chunk=make_read_only_after("store", 1),
    ) as cache:
        written = cache.store_many(artifacts, accessed_at=LOOKUP)

    # Committed chunks hold correct artifacts under their own fingerprints;
    # failed chunks wrote nothing. Either way every stored row is valid.
    assert written == 10
    assert (stats.failure, stats.store_chunks) == ("store", 3)
    present = {row[0] for row in dump(path)}
    assert {fp(i) for i in range(40, 50)} <= present
    assert not {fp(i) for i in range(50, 70)} & present
    assert integrity(path) == "ok"

    # Retry (the next cold run stores everything that is still missing).
    with ChunkedAnalysisCache(path, cache_version=VERSION, chunk_rows=10) as cache:
        assert cache.store_many(artifacts, accessed_at=LOOKUP) == 30
    with AnalysisCache(reference, cache_version=VERSION) as cache:
        assert cache.store_many(artifacts, accessed_at=LOOKUP) == 30
    assert dump(path) == dump(reference)


# --- hosted analyzer end to end ---------------------------------------------


def hosted_rows() -> list[dict]:
    base = dict(
        source="direct",
        source_adapter="workday",
        location="New York, NY",
        compensation="$40/hr",
        description="Build Python services with mentorship and code review.",
        requirements="Python, SQL, Git. Pursuing a bachelor's degree.",
        date_posted="2026-08-30",
        internship_type="Summer 2027 Internship",
    )
    titles = [
        "Software Engineering Intern",
        "Data Science Intern",
        "Backend Engineering Intern",
        "Quant Research Intern",
        "Account Executive",
    ]
    rows = [
        make_row(company="Example Co", title=title,
                 source_url=f"https://careers.example.test/jobs/{i}", **base)
        for i, title in enumerate(titles)
    ]
    return [*rows, copy.deepcopy(rows[0])]  # a duplicate exercises dedupe


def analyze(timing=None) -> str:
    analyzer = hosted_cache.build_hosted_analyzer(timing)
    jobs = analyzer(hosted_rows(), today=LOOKUP.date())
    return json.dumps(jobs, sort_keys=True, default=str)


def cache_fields(timing: HostedTiming) -> dict[str, str]:
    lines = [line for line in timing.lines(exit_code=0) if " kind=cache " in f" {line} "]
    assert len(lines) == 1
    assert LINE_RE.fullmatch(lines[0])
    return dict(part.split("=", 1) for part in lines[0].split()[2:])


def uncached() -> str:
    return json.dumps(analyze_rows(hosted_rows(), today=LOOKUP.date()), sort_keys=True, default=str)


def test_hosted_analyzer_chunks_by_default_with_identical_jobs(tmp_path, monkeypatch):
    path = tmp_path / "volume" / "cache.sqlite"
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    monkeypatch.setenv(chunked_cache.CHUNK_ENV, "2")
    expected = uncached()

    cold_timing, warm_timing = HostedTiming(), HostedTiming()
    assert analyze(cold_timing) == expected
    assert analyze(warm_timing) == expected

    cold, warm = cache_fields(cold_timing), cache_fields(warm_timing)
    assert (cold["hits"], cold["misses"], cold["writes"]) == ("0", "5", "5")
    assert (cold["chunk_rows"], cold["lookup_chunks"], cold["store_chunks"]) == ("2", "3", "3")
    assert (warm["hits"], warm["misses"], warm["writes"]) == ("5", "0", "0")
    assert (warm["lookup_chunks"], warm["store_chunks"], warm["chunk_failure"]) == ("3", "0", "none")
    assert integrity(path) == "ok"


def test_default_setting_passes_a_ten_thousand_row_chunked_factory(tmp_path, monkeypatch):
    path = tmp_path / "volume" / "cache.sqlite"
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    seen = {}
    real = hosted_cache.analyze_rows_with_cache

    def spy(rows, **kwargs):
        seen.update(kwargs)
        return real(rows, **kwargs)

    monkeypatch.setattr(hosted_cache, "analyze_rows_with_cache", spy)
    timing = HostedTiming()
    assert analyze(timing) == uncached()
    cache = seen["cache_factory"](path, cache_version=VERSION)
    try:
        assert isinstance(cache, ChunkedAnalysisCache) and cache.chunk_rows == 10_000
    finally:
        cache.close()
    assert cache_fields(timing)["chunk_rows"] == "10000"


def test_zero_restores_the_exact_deployed_call(tmp_path, monkeypatch):
    path = tmp_path / "volume" / "cache.sqlite"
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    monkeypatch.setenv(chunked_cache.CHUNK_ENV, "0")
    seen = {}
    real = hosted_cache.analyze_rows_with_cache

    def spy(rows, **kwargs):
        seen.update(kwargs)
        return real(rows, **kwargs)

    monkeypatch.setattr(hosted_cache, "analyze_rows_with_cache", spy)
    timing = HostedTiming()
    assert analyze(timing) == uncached()
    assert "cache_factory" not in seen
    fields = cache_fields(timing)
    assert (fields["chunk_rows"], fields["lookup_chunks"], fields["store_chunks"]) == ("0", "0", "0")


def test_invalid_setting_uses_the_default_and_names_only_a_reason(tmp_path, monkeypatch, caplog):
    path = tmp_path / "volume" / "cache.sqlite"
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    monkeypatch.setenv(chunked_cache.CHUNK_ENV, "lots-of-rows")
    timing = HostedTiming()
    assert analyze(timing) == uncached()
    assert cache_fields(timing)["chunk_rows"] == "10000"
    assert "invalid_chunk_rows" in caplog.text
    assert "lots-of-rows" not in caplog.text


def test_failing_lookup_chunk_falls_back_to_identical_jobs_then_recovers(tmp_path, monkeypatch):
    path = tmp_path / "volume" / "cache.sqlite"
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    monkeypatch.setenv(chunked_cache.CHUNK_ENV, "2")
    expected = uncached()
    assert analyze() == expected  # cold: fills the cache

    real = AnalysisCache.lookup_many
    calls = {"n": 0}

    def second_chunk_fails(self, fingerprints, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            return CacheLookupResult({}, failed=True)
        return real(self, fingerprints, **kwargs)

    monkeypatch.setattr(AnalysisCache, "lookup_many", second_chunk_fails)
    failed_timing = HostedTiming()
    assert analyze(failed_timing) == expected
    fields = cache_fields(failed_timing)
    assert (fields["hits"], fields["misses"], fields["chunk_failure"]) == ("0", "5", "lookup")
    assert integrity(path) == "ok"

    monkeypatch.setattr(AnalysisCache, "lookup_many", real)
    retry_timing = HostedTiming()
    assert analyze(retry_timing) == expected
    retry = cache_fields(retry_timing)
    assert (retry["hits"], retry["misses"], retry["chunk_failure"]) == ("5", "0", "none")


def test_chunked_cache_is_not_part_of_the_cache_version():
    assert "app.hosted.chunked_cache" not in hosted_cache.STATIC_ANALYSIS_MODULES
    assert "app.hosted.analysis_cache" not in hosted_cache.STATIC_ANALYSIS_MODULES
    assert hosted_cache.hosted_static_cache_version() is not None


def test_chunk_telemetry_never_raises_on_malformed_values():
    timing = HostedTiming()
    timing.record_analysis_cache(object(), chunking=object())
    timing.record_analysis_cache(None, reason="cache_failed", chunking=ChunkStats(chunk_rows=5))
    lines = [line for line in timing.lines(exit_code=0) if " kind=cache " in f" {line} "]
    assert len(lines) == 1 and LINE_RE.fullmatch(lines[0])
    assert os.sep not in lines[0]
