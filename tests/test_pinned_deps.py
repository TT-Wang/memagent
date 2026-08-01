"""The supply-chain gate (scripts/check_pinned_deps.py): ranges, wildcard pins, lock drift, and a
missing lock are all violations; an exact pin aligned with uv.lock passes.

No model, no pytest. Run: python tests/test_pinned_deps.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

_spec = importlib.util.spec_from_file_location(
    "check_pinned_deps",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "check_pinned_deps.py"))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

CHECKS = []
def check(fn):
    CHECKS.append(fn)
    return fn


_LOCK = """version = 1

[[package]]
name = "foo"
version = "2.0"

[[package]]
name = "bar"
version = "1.4"
"""


def _stage(pyproject: str, lock: str | None) -> None:
    from pathlib import Path
    root = Path(tempfile.mkdtemp(prefix="pinned-"))
    mod.PYPROJECT = root / "pyproject.toml"
    mod.PYPROJECT.write_text(pyproject, encoding="utf-8")
    mod.UV_LOCK = root / "uv.lock"
    if lock is not None:
        mod.UV_LOCK.write_text(lock, encoding="utf-8")


def _pyproject(*deps: str) -> str:
    body = "\n".join(f'  "{d}",' for d in deps)
    return f'[project]\nname = "demo"\nversion = "0.0"\ndependencies = [\n{body}\n]\n'


@check
def an_exact_pin_aligned_with_the_lock_passes():
    _stage(_pyproject("foo==2.0", "bar==1.4"), _LOCK)
    assert mod.violations() == [], mod.violations()


@check
def a_range_is_a_violation():
    _stage(_pyproject("foo>=2.0"), _LOCK)
    problems = mod.violations()
    assert any("EXACT-pinned" in p for p in problems), problems


@check
def a_wildcard_pin_is_a_range_not_a_pin():
    """The review's P5b: startswith('==') let '==2.*' pass — a range wearing a pin's clothes."""
    _stage(_pyproject("foo==2.*"), _LOCK)
    problems = mod.violations()
    assert any("wildcard" in p for p in problems), problems


@check
def manifest_lock_drift_is_a_violation():
    """The review's P5b: the gate never opened uv.lock despite its docstring promising it."""
    _stage(_pyproject("foo==1.0"), _LOCK)   # lock resolved 2.0
    problems = mod.violations()
    assert any("uv.lock resolved" in p for p in problems), problems


@check
def a_missing_lock_is_a_violation_not_a_pass():
    _stage(_pyproject("foo==2.0"), None)
    problems = mod.violations()
    assert any("uv.lock" in p for p in problems), problems


if __name__ == "__main__":
    ok = 0
    for fn in CHECKS:
        try:
            fn()
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(CHECKS)} passed")
    sys.exit(0 if ok == len(CHECKS) else 1)
