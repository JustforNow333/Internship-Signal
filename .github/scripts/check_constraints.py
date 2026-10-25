"""Fail CI unless the installed packages are exactly the pinned constraints.

requirements.txt applies backend/constraints.txt, so every package that is
installed must be pinned there at the installed version, and every pin must
be installed. Usage:

    python .github/scripts/check_constraints.py backend/constraints.txt
"""

from __future__ import annotations

import re
import sys
from importlib import metadata

# Installer tooling comes from the Python image, not from the lock.
UNPINNED = frozenset({"pip", "setuptools", "wheel"})


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def main(path: str) -> int:
    pinned: dict[str, str] = {}
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            name, separator, version = line.partition("==")
            if not separator:
                print(f"Not an exact pin: {line}")
                return 1
            pinned[normalize(name)] = version.strip()
    installed = {
        normalize(dist.metadata["Name"]): dist.version
        for dist in metadata.distributions()
    }
    problems = [
        f"installed but not pinned: {name}=={version}"
        for name, version in sorted(installed.items())
        if name not in pinned and name not in UNPINNED
    ]
    problems += [
        f"pinned {name}=={version} but installed {installed[name]}"
        for name, version in sorted(pinned.items())
        if name in installed and installed[name] != version
    ]
    problems += [
        f"pinned but not installed: {name}=={version}"
        for name, version in sorted(pinned.items())
        if name not in installed
    ]
    for problem in problems:
        print(problem)
    print(f"{len(pinned)} pins, {len(installed)} installed, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
