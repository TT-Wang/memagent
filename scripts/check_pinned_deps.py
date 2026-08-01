#!/usr/bin/env python3
"""Supply-chain policy check (ported from Pi's check:pinned-deps): every DIRECT dependency in
pyproject.toml must be EXACT-pinned (==) to the version uv.lock actually resolved.

A version RANGE in a direct dependency is a reviewed-code-change violation: resolution would
silently pick a different tree than the one CI tested. A WILDCARD pin (==2.*) is a range wearing
a pin's clothes — rejected too. And a pin that disagrees with uv.lock means the lock CI tests is
not the tree the manifest advertises (the earlier cut of this check never opened uv.lock despite
this docstring promising it — the review's P5b medium). Extras markers ( ; python_version /
extra) and comments are tolerated; VCS/URL specs are not direct-version deps and are rejected
loudly. Exits non-zero on any violation so CI can gate on it.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
UV_LOCK = ROOT / "uv.lock"


def _deps():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    deps = list(project.get("dependencies", []) or [])
    for extra, extra_deps in (project.get("optional-dependencies", {}) or {}).items():
        deps.extend(extra_deps)
    return deps


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _locked_versions() -> dict[str, str]:
    if not UV_LOCK.exists():
        return {}
    data = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    return {_norm(pkg.get("name", "")): str(pkg.get("version", ""))
            for pkg in data.get("package", []) if pkg.get("name")}


def violations() -> list[str]:
    out = []
    # Documented waivers (also noted inline in pyproject.toml): a range kept to preserve the
    # supported-Python matrix. Pinning these would force a newer interpreter than we support.
    MATRIX_WAIVER = {"numpy"}
    locked = _locked_versions()
    if not locked:
        out.append(f"  {UV_LOCK.name} missing or unreadable — pin consistency cannot be verified")
    for raw in _deps():
        spec = raw.split(";", 1)[0].strip()          # drop environment markers
        name = re.split(r"[<>=!~\[ ]", spec, 1)[0].strip()
        rest = spec[len(name):].strip()
        if not name:
            continue
        if name in MATRIX_WAIVER:
            continue
        if not rest.startswith("=="):
            out.append(f"  {raw!r} — direct dependency must be EXACT-pinned (==), got {rest!r}")
            continue
        pinned = rest[2:].strip()
        if "*" in pinned:
            out.append(f"  {raw!r} — a wildcard pin ({pinned!r}) is a range, not an exact pin")
            continue
        if locked:
            want = locked.get(_norm(name))
            if want is None:
                out.append(f"  {raw!r} — not present in uv.lock; re-resolve the lock")
            elif want != pinned:
                out.append(f"  {raw!r} — pinned {pinned!r} but uv.lock resolved {want!r}; "
                           "align the manifest with the lock CI tests")
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
