"""Fail CI when pytest skipped anything beyond the known, intentional skips.

Hosted PostgreSQL tests skip themselves when their database is unusable, so
an unexpected skip means CI silently lost coverage. Usage:

    python .github/scripts/check_skips.py pytest-results.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ElementTree

# (JUnit classname, test name without parameters): why it is skipped in CI.
EXPECTED_SKIPS = {
    (
        "backend.tests.test_hosted_notifications",
        "test_verified_snapshot_notification_smoke",
    ): "needs HOSTED_NOTIFICATION_SMOKE_SNAPSHOT, a private real snapshot",
    (
        "watcher.tests.test_finance_expansion_batch_2",
        "test_fallback_batch_companies_are_backstop_only",
    ): "FALLBACK_BATCH_COMPANIES is empty since Optiver became a direct source",
}


def main(path: str) -> int:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        print(f"No usable pytest results at {path}: {exc}")
        return 1
    cases = list(root.iter("testcase"))
    skipped = {
        (case.get("classname", ""), case.get("name", "").split("[", 1)[0])
        for case in cases
        if case.find("skipped") is not None
    }
    print(f"{len(cases)} tests reported, {len(skipped)} skipped")
    if not cases:
        print("No tests ran.")
        return 1
    for key in sorted(skipped & EXPECTED_SKIPS.keys()):
        print(f"expected skip: {'::'.join(key)} ({EXPECTED_SKIPS[key]})")
    for key in sorted(EXPECTED_SKIPS.keys() - skipped):
        print(f"note: no longer skipped: {'::'.join(key)}")
    unexpected = sorted(skipped - EXPECTED_SKIPS.keys())
    for key in unexpected:
        print(f"UNEXPECTED SKIP: {'::'.join(key)}")
    return 1 if unexpected else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
