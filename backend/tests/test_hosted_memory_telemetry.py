"""Offline coverage for best-effort hosted memory telemetry.

Memory readings come from fixed kernel files: process RSS and high-water mark
from ``/proc/self/status`` and container usage from cgroup files. Every reader
here points at fixture files, so the results are deterministic and the tests
never depend on the machine running them. Readings are counts in MiB only and
can never fail or change a collection run.
"""

from __future__ import annotations

import re

import pytest
from app.hosted import collect_and_import
from app.hosted import memory_telemetry
from app.hosted.collect_and_import import main as collect_main
from app.hosted.memory_telemetry import MEMORY_FIELDS, sample_memory
from app.hosted.timing import PREFIX, HostedTiming

LINE_RE = re.compile(r"^HOSTED-TIMING kind=[a-z]+( [a-z_]+=[a-z0-9_.]+)+$")
MIB = 1024 * 1024

PROC_STATUS = """\
Name:\tpython
VmPeak:\t 9000000 kB
VmHWM:\t 2097152 kB
VmRSS:\t 1048576 kB
Threads:\t4
"""

V2_STAT = """\
anon 3221225472
file 1073741824
kernel 1048576
shmem 0
"""


def _proc(tmp_path, *, status=PROC_STATUS, cgroup="0::/\n"):
    status_path = tmp_path / "status"
    status_path.write_text(status, encoding="utf-8")
    cgroup_path = tmp_path / "cgroup"
    cgroup_path.write_text(cgroup, encoding="utf-8")
    return status_path, cgroup_path


def _v2(root, *, current=5 * 1024 * MIB, peak=8 * 1024 * MIB, stat=V2_STAT):
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.current").write_text(f"{current}\n", encoding="utf-8")
    if peak is not None:
        (root / "memory.peak").write_text(f"{peak}\n", encoding="utf-8")
    if stat is not None:
        (root / "memory.stat").write_text(stat, encoding="utf-8")


def _sample(tmp_path, cgroup_root, **proc):
    status_path, cgroup_path = _proc(tmp_path, **proc)
    return sample_memory(
        proc_status=status_path,
        proc_cgroup=cgroup_path,
        cgroup_root=cgroup_root,
    )


# --- readers ------------------------------------------------------------------


def test_reads_process_and_cgroup_v2_memory_in_mib(tmp_path) -> None:
    root = tmp_path / "sys"
    _v2(root)

    assert _sample(tmp_path, root) == {
        "rss_mb": 1024,
        "hwm_mb": 2048,
        "cgroup_current_mb": 5120,
        "cgroup_peak_mb": 8192,
        "cgroup_anon_mb": 3072,
        "cgroup_file_mb": 1024,
    }


def test_fields_follow_one_fixed_order(tmp_path) -> None:
    root = tmp_path / "sys"
    _v2(root)

    assert tuple(_sample(tmp_path, root)) == MEMORY_FIELDS


def test_a_nested_cgroup_v2_path_is_preferred_over_the_mount_root(tmp_path) -> None:
    root = tmp_path / "sys"
    _v2(root, current=1 * MIB)
    _v2(root / "railway" / "service", current=7 * MIB)

    fields = _sample(tmp_path, root, cgroup="0::/railway/service\n")

    assert fields["cgroup_current_mb"] == 7


def test_cgroup_v1_is_used_when_v2_is_absent(tmp_path) -> None:
    root = tmp_path / "sys"
    memory = root / "memory"
    memory.mkdir(parents=True)
    (memory / "memory.usage_in_bytes").write_text(f"{6 * MIB}\n", encoding="utf-8")
    (memory / "memory.max_usage_in_bytes").write_text(f"{9 * MIB}\n", encoding="utf-8")
    (memory / "memory.stat").write_text(
        f"cache {2 * MIB}\nrss {3 * MIB}\ntotal_cache {4 * MIB}\ntotal_rss {5 * MIB}\n",
        encoding="utf-8",
    )

    fields = _sample(tmp_path, root, cgroup="5:memory:/\n")

    assert fields == {
        "rss_mb": 1024,
        "hwm_mb": 2048,
        "cgroup_current_mb": 6,
        "cgroup_peak_mb": 9,
        "cgroup_anon_mb": 5,
        "cgroup_file_mb": 4,
    }


def test_missing_sources_are_omitted_rather_than_guessed(tmp_path) -> None:
    root = tmp_path / "sys"
    _v2(root, peak=None, stat=None)

    fields = _sample(tmp_path, root, status="Name:\tpython\nVmRSS:\t 2048 kB\n")

    assert fields == {"rss_mb": 2, "cgroup_current_mb": 5120}


@pytest.mark.parametrize(
    "status",
    ["", "VmRSS:\tlots kB\n", "VmRSS:\t-5 kB\n", "\x00\xff garbage"],
)
def test_malformed_process_status_never_raises(tmp_path, status) -> None:
    fields = _sample(tmp_path, tmp_path / "absent", status=status)

    assert "rss_mb" not in fields
    assert "hwm_mb" not in fields


def test_malformed_cgroup_files_never_raise(tmp_path) -> None:
    root = tmp_path / "sys"
    root.mkdir()
    (root / "memory.current").write_text("max\n", encoding="utf-8")
    (root / "memory.stat").write_text("anon\nfile x\n", encoding="utf-8")

    fields = _sample(tmp_path, root)

    assert set(fields) <= {"rss_mb", "hwm_mb"}


def test_unreadable_everything_returns_an_empty_reading(tmp_path) -> None:
    missing = tmp_path / "missing"

    assert (
        sample_memory(
            proc_status=missing / "status",
            proc_cgroup=missing / "cgroup",
            cgroup_root=missing,
        )
        == {}
    )


# --- HostedTiming integration --------------------------------------------------


def _fixed_sampler(**fields):
    return lambda: dict(fields)


def test_memory_lines_are_off_unless_a_sampler_is_configured() -> None:
    timing = HostedTiming(clock=lambda: 0.0)
    with timing.stage("analysis"):
        pass

    assert not any("kind=memory" in line for line in timing.lines(exit_code=0))


def test_each_timed_stage_and_the_total_report_memory_in_stage_order() -> None:
    readings = iter(
        [
            {"rss_mb": 300, "hwm_mb": 900, "cgroup_current_mb": 1000},
            {"rss_mb": 100, "hwm_mb": 100, "cgroup_current_mb": 200},
            {"rss_mb": 50, "hwm_mb": 900, "cgroup_current_mb": 400},
        ]
    )
    timing = HostedTiming(clock=lambda: 0.0)
    timing.use_memory_sampler(lambda: next(readings))
    with timing.stage("analysis"):
        pass
    with timing.stage("collection"):
        pass
    timing.sample_memory("total")

    lines = timing.lines(exit_code=0)
    memory = [line for line in lines if "kind=memory" in line]
    assert memory == [
        f"{PREFIX} kind=memory stage=collection rss_mb=100 hwm_mb=100 cgroup_current_mb=200",
        f"{PREFIX} kind=memory stage=analysis rss_mb=300 hwm_mb=900 cgroup_current_mb=1000",
        f"{PREFIX} kind=memory stage=total rss_mb=50 hwm_mb=900 cgroup_current_mb=400",
    ]
    assert lines[-1].startswith(f"{PREFIX} kind=stage stage=total ")
    assert all(LINE_RE.match(line) for line in lines)


def test_unknown_fields_and_unknown_stages_are_never_emitted() -> None:
    timing = HostedTiming(clock=lambda: 0.0)
    timing.use_memory_sampler(
        _fixed_sampler(rss_mb=5, company="ZephyrQuillWorks", path="/data/x")
    )
    with timing.stage("identity_shadow"):
        pass
    with timing.stage("analysis"):
        pass

    memory = [line for line in timing.lines(exit_code=0) if "kind=memory" in line]
    assert memory == [f"{PREFIX} kind=memory stage=analysis rss_mb=5"]


def test_an_empty_reading_emits_no_memory_line() -> None:
    timing = HostedTiming(clock=lambda: 0.0)
    timing.use_memory_sampler(dict)
    with timing.stage("analysis"):
        pass

    assert not any("kind=memory" in line for line in timing.lines(exit_code=0))


@pytest.mark.parametrize(
    "sampler",
    [
        lambda: (_ for _ in ()).throw(OSError("proc unavailable")),
        lambda: "not a mapping",
        lambda: {"rss_mb": "lots"},
        None,
    ],
)
def test_a_broken_sampler_never_changes_stage_timing(sampler) -> None:
    ticks = iter(range(100))
    timing = HostedTiming(clock=lambda: float(next(ticks)))
    timing.use_memory_sampler(sampler)
    with timing.stage("analysis"):
        pass
    timing.sample_memory("total")

    stage_lines = [line for line in timing.lines(exit_code=0) if "kind=stage" in line]
    assert stage_lines[0] == f"{PREFIX} kind=stage stage=analysis seconds=1.000"
    assert stage_lines[-1].endswith("exit_code=0")


def test_a_stage_that_raises_still_samples_memory() -> None:
    timing = HostedTiming(clock=lambda: 0.0)
    timing.use_memory_sampler(_fixed_sampler(rss_mb=7))
    with pytest.raises(ValueError):
        with timing.stage("analysis"):
            raise ValueError("boom")

    assert f"{PREFIX} kind=memory stage=analysis rss_mb=7" in timing.lines(exit_code=1)


# --- scheduled entry point -----------------------------------------------------


def test_the_scheduled_command_samples_memory_with_the_kernel_reader(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("HOSTED_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        memory_telemetry, "sample_memory", _fixed_sampler(rss_mb=11, hwm_mb=12)
    )

    assert collect_main([], collector=lambda _config: None) == 2

    lines = capsys.readouterr().out.splitlines()
    assert lines[-2] == f"{PREFIX} kind=memory stage=total rss_mb=11 hwm_mb=12"
    assert lines[-1].startswith(f"{PREFIX} kind=stage stage=total ")
    assert lines[-1].endswith("exit_code=2")


def test_a_failing_kernel_reader_never_changes_the_command_result(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("HOSTED_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def broken():
        raise MemoryError("cannot read")

    monkeypatch.setattr(memory_telemetry, "sample_memory", broken)

    assert collect_main([], collector=lambda _config: None) == 2

    captured = capsys.readouterr()
    assert captured.err == "Hosted collection failed: hosted_database_not_configured\n"
    assert not any("kind=memory" in line for line in captured.out.splitlines())
    assert captured.out.splitlines()[-1].endswith("exit_code=2")


def test_the_entry_point_never_constructs_timing_with_new_arguments(
    monkeypatch, capsys
) -> None:
    # Existing tests replace ``HostedTiming`` with a zero-argument factory;
    # enabling memory sampling must not change how the command constructs it.
    monkeypatch.delenv("HOSTED_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        collect_and_import, "HostedTiming", lambda: HostedTiming(clock=lambda: 0.0)
    )

    assert collect_main([], collector=lambda _config: None) == 2
    assert capsys.readouterr().out.splitlines()[-1].endswith("exit_code=2")
