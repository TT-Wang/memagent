#!/usr/bin/env python3
"""Registration guard for the script-style test suite.

A test DEFINED BELOW its file's ``if __name__ == "__main__":`` block never executes: the runner
fires while the module body is still above it, iterates a registry that does not contain the
test, and exits — the suite reports green in BOTH directions (a planted failure changes
nothing). The review found the mid-turn-slash check (U2a/c) dead in exactly this shape while
the tally claimed it covered. This guard fails the gate if any test file has a registrable
definition below its runner block.

Exit 0 = clean, exit 1 = violations (each printed).
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_ROOTS = (
    REPO_ROOT / "tests",
    *sorted((REPO_ROOT / "packages").glob("*/tests")),
)

# Shapes that register a test when the module body reaches them: the @check decorator pattern
# and bare pytest-style test definitions.
_REGISTRABLE = re.compile(r"^(@check\b|def test_\w+\()")


def violations(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    runner = next((i for i, line in enumerate(lines) if line.startswith('if __name__')), None)
    if runner is None:
        return []
    return [f"{path.name}:{i + 1}: {lines[i].strip()[:60]} registers below the runner (line {runner + 1})"
            for i in range(runner + 1, len(lines)) if _REGISTRABLE.match(lines[i])]


def main() -> int:
    found = []
    paths = sorted(path for root in TEST_ROOTS for path in root.glob("test_*.py"))
    for path in paths:
        found.extend(violations(path))
    for line in found:
        print(f"DEAD TEST: {line}")
    if found:
        print(f"{len(found)} registrable definition(s) below a runner block — they never execute")
        return 1
    print(f"test registration: clean ({len(paths)} files across root + package tests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
