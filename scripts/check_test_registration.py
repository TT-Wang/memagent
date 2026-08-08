#!/usr/bin/env python3
"""Registration guard for the script-style test suite.

A test DEFINED BELOW its file's ``if __name__ == "__main__":`` block never executes: the runner
fires while the module body is still above it, iterates a registry that does not contain the
test, and exits — the suite reports green in BOTH directions (a planted failure changes
nothing). The review found the mid-turn-slash check (U2a/c) dead in exactly this shape while
the tally claimed it covered. This guard fails the gate if any test file has a registrable
definition below its runner block.

Second dead shape (2026-08-08 review M10): a HYBRID file (one with a runner block) whose runner
uses an explicit tuple of tests. run_tests.sh routes any file containing ``if __name__`` to
script mode and NEVER to pytest, so a pytest-style ``def test_x`` defined ABOVE the runner but
not named in the runner tuple is neither executed nor flagged by the below-runner check. The
guard now requires every pytest-style def above the runner to be referenced somewhere else in
the file (the runner tuple, a caller, a string) — an unreferenced def can only ever execute
through pytest collection, which hybrid files never get.

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
# and bare pytest-style test definitions (column-0, and indented class-method spellings — pytest
# would collect those, but a hybrid file never reaches pytest).
_REGISTRABLE = re.compile(r"^(@check\b|def test_\w+\()")
_INDENTED_TEST = re.compile(r"^\s+def test_\w+\(")
_DEF_TEST = re.compile(r"^def (test_\w+)\(")


def violations(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    runner = next((i for i, line in enumerate(lines) if line.startswith('if __name__')), None)
    if runner is None:
        return []   # pure pytest-style file: pytest collects every def test_* — nothing to check
    out = []
    for i in range(runner + 1, len(lines)):
        if _REGISTRABLE.match(lines[i]) or _INDENTED_TEST.match(lines[i]):
            out.append(f"{path.name}:{i + 1}: {lines[i].strip()[:60]} registers below the runner (line {runner + 1})")
    # main() may live ABOVE the runner (``if __name__: main()`` is the whole block), so look for the
    # globals()-iteration pattern ANYWHERE in the file: such a runner executes every test_* defined
    # above it by name-discovery, so the explicit-tuple dead-def check does not apply.
    if re.search(r"globals\(\)\.items\(\).*startswith\(\s*[\"']test_[\"']\s*\)", "\n".join(lines), re.DOTALL):
        return out
    for i in range(0, runner):
        match = _DEF_TEST.match(lines[i])
        if match is None:
            continue
        name = match.group(1)
        if i > 0 and lines[i - 1].startswith("@"):
            continue   # decorator-registered (CHECKS registry): main() runs it without naming it
        # count occurrences of the bare name in every line EXCEPT this definition line
        occurrences = sum(
            1 for j, line in enumerate(lines)
            if j != i and re.search(rf"\b{name}\b", line)
        )
        if occurrences == 0:
            out.append(
                f"{path.name}:{i + 1}: {name}() is defined above the runner but never referenced — "
                f"the explicit runner tuple never calls it and pytest never collects hybrid files")
    return out


def main() -> int:
    found = []
    paths = sorted(path for root in TEST_ROOTS for path in root.glob("test_*.py"))
    for path in paths:
        found.extend(violations(path))
    for line in found:
        print(f"DEAD TEST: {line}")
    if found:
        print(f"{len(found)} registrable definition(s) never execute")
        return 1
    print(f"test registration: clean ({len(paths)} files across root + package tests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
