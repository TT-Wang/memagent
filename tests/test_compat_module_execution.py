"""Every umbrella module is an import alias, never a silent ``python -m`` no-op."""
from __future__ import annotations

import re
from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parent.parent
SHIM_ROOT = REPO / "src" / "sliceagent"
TARGET = re.compile(r'_alias_module\(__name__, __spec__, "(?P<target>sliceagent_(?:core|cli)\.[^"]+)"\)')


def _shim_targets() -> dict[str, str]:
    targets = {}
    for path in sorted(SHIM_ROOT.glob("*.py")):
        if path.name in {"__init__.py", "__main__.py", "_compat.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        match = TARGET.search(source)
        assert match is not None, f"legacy module lacks the shared execution guard: {path.name}"
        targets[f"sliceagent.{path.stem}"] = match.group("target")
    assert targets, "no legacy module shims discovered"
    return targets


def test_every_legacy_module_execution_fails_loudly_with_the_new_path():
    for legacy, target in _shim_targets().items():
        result = subprocess.run(
            [sys.executable, "-m", legacy],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, (legacy, result.returncode, result.stdout, result.stderr)
        assert not result.stdout, (legacy, result.stdout)
        assert legacy in result.stderr and target in result.stderr, (legacy, result.stderr)


def test_regular_legacy_imports_still_preserve_exact_module_identity():
    import sliceagent.active_work as legacy_core
    import sliceagent.tools as legacy_cli
    import sliceagent_cli.tools as cli_tools
    import sliceagent_core.active_work as core_active_work

    assert legacy_core is core_active_work
    assert legacy_cli is cli_tools
