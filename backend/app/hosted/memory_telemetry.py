"""Best-effort process and container memory readings for ``HOSTED-TIMING``.

``collect_and_import`` samples these at the end of each timed stage so a run's
memory profile is measurable from its logs: process resident memory and its
high-water mark from ``/proc/self/status``, plus the container cgroup's current
and peak usage split into anonymous memory and reclaimable file (page) cache.

Readings are whole MiB counts under fixed field names - never paths, process
names, or anything else read from the files. Every read is a small kernel file;
any missing, unreadable, or malformed source is simply omitted, and sampling
never raises.
"""

from __future__ import annotations

from pathlib import Path

PROC_STATUS = Path("/proc/self/status")
PROC_CGROUP = Path("/proc/self/cgroup")
CGROUP_ROOT = Path("/sys/fs/cgroup")

# Canonical emission order of every field this module can report.
MEMORY_FIELDS = (
    "rss_mb",
    "hwm_mb",
    "cgroup_current_mb",
    "cgroup_peak_mb",
    "cgroup_anon_mb",
    "cgroup_file_mb",
)

_MIB = 1024 * 1024
_KIB_PER_MIB = 1024
# Bounds every read so a surprising file can never cost real memory.
_MAX_READ_BYTES = 64 * 1024


def sample_memory(
    *,
    proc_status: Path = PROC_STATUS,
    proc_cgroup: Path = PROC_CGROUP,
    cgroup_root: Path = CGROUP_ROOT,
) -> dict[str, int]:
    """Return the available readings in ``MEMORY_FIELDS`` order. Never raises."""

    fields: dict[str, int] = {}
    try:
        fields.update(_process_fields(proc_status))
    except Exception:  # noqa: BLE001 - telemetry must never raise
        pass
    try:
        fields.update(_cgroup_fields(proc_cgroup, cgroup_root))
    except Exception:  # noqa: BLE001
        pass
    return {name: fields[name] for name in MEMORY_FIELDS if name in fields}


def _read(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            return stream.read(_MAX_READ_BYTES).decode("ascii", errors="strict")
    except Exception:  # noqa: BLE001
        return None


def _non_negative(text: str) -> int | None:
    try:
        value = int(text.strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _process_fields(path: Path) -> dict[str, int]:
    text = _read(path)
    if text is None:
        return {}
    labels = {"VmRSS": "rss_mb", "VmHWM": "hwm_mb"}
    fields: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        name = labels.get(key.strip())
        if name is None or not separator:
            continue
        parts = value.split()
        if len(parts) != 2 or parts[1] != "kB":
            continue
        kib = _non_negative(parts[0])
        if kib is not None:
            fields[name] = kib // _KIB_PER_MIB
    return fields


def _stat_values(path: Path) -> dict[str, int]:
    text = _read(path)
    if text is None:
        return {}
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            number = _non_negative(parts[1])
            if number is not None:
                values[parts[0]] = number
    return values


def _bytes_file(path: Path) -> int | None:
    text = _read(path)
    return _non_negative(text) if text is not None else None


def _cgroup_paths(proc_cgroup: Path) -> tuple[str | None, str | None]:
    """Return this process's (v2 unified, v1 memory) cgroup paths, if listed."""

    text = _read(proc_cgroup) or ""
    unified = memory = None
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        hierarchy, controllers, path = parts
        if hierarchy == "0" and controllers == "":
            unified = path
        elif "memory" in controllers.split(","):
            memory = path
    return unified, memory


def _candidates(root: Path, path: str | None) -> list[Path]:
    relative = (path or "").strip().strip("/")
    # Inside a cgroup namespace the listed path may not exist under the mount;
    # the mount root itself is then this container's cgroup.
    nested = [root / relative] if relative and ".." not in relative.split("/") else []
    return [*nested, root]


def _cgroup_fields(proc_cgroup: Path, cgroup_root: Path) -> dict[str, int]:
    unified, memory = _cgroup_paths(proc_cgroup)
    for directory in _candidates(cgroup_root, unified):
        current = _bytes_file(directory / "memory.current")
        if current is None:
            continue
        stat = _stat_values(directory / "memory.stat")
        return _mib_fields(
            current=current,
            peak=_bytes_file(directory / "memory.peak"),
            anon=stat.get("anon"),
            file=stat.get("file"),
        )
    for directory in _candidates(cgroup_root / "memory", memory):
        current = _bytes_file(directory / "memory.usage_in_bytes")
        if current is None:
            continue
        stat = _stat_values(directory / "memory.stat")
        return _mib_fields(
            current=current,
            peak=_bytes_file(directory / "memory.max_usage_in_bytes"),
            anon=stat.get("total_rss", stat.get("rss")),
            file=stat.get("total_cache", stat.get("cache")),
        )
    return {}


def _mib_fields(
    *,
    current: int,
    peak: int | None,
    anon: int | None,
    file: int | None,
) -> dict[str, int]:
    values = {
        "cgroup_current_mb": current,
        "cgroup_peak_mb": peak,
        "cgroup_anon_mb": anon,
        "cgroup_file_mb": file,
    }
    return {name: value // _MIB for name, value in values.items() if value is not None}
