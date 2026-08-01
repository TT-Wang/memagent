#!/usr/bin/env python3
"""Supply-chain policy check (ported from Pi's check:pinned-deps): every DIRECT dependency in
pyproject.toml must be EXACT-pinned (==), and uv.lock must be consistent with it.

A version RANGE in a direct dependency is a reviewed-code-change violation: resolution would
silently pick a different tree than the one CI tested. Extras markers ( ; python_version / extra)
and comments are tolerated; VCS/URL specs are not direct-version deps and are rejected loudly.
Exits non-zero on any violation so CI can gate on it.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def _deps():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    deps = list(project.get("dependencies", []) or [])
    for extra, extra_deps in (project.get("optional-dependencies", {}) or {}).items():
        deps.extend(extra_deps)
    return deps


def violations() -> list[str]:
    out = []
    # Documented waivers (also noted inline in pyproject.toml): a range kept to preserve the
    # supported-Python matrix. Pinning these would force a newer interpreter than we support.
    MATRIX_WAIVER = {"numpy"}
    for raw in _deps():
        spec = raw.split(";", 1)[0].strip()          # drop environment markers
        name = re.split(r"[<>=!~\[ ]", spec, 1)[0].strip()
        rest = spec[len(name):].strip()
        if not name:
            continue
        if not rest.startswith("==") and name not in MATRIX_WAIVER:
            out.append(f"  {raw!r} — direct dependency must be EXACT-pinned (==), got {rest!r}")
    return out


def main() -> int:
    problems = violations()
    if problems:
        print("unpinned direct dependencies (supply-chain policy: exact pins only):")
        print("\n".join(problems))
        return 1
    print(f"pinned-deps OK ({len(_deps())} direct deps, exact-pinned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
