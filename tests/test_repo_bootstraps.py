"""Repo-local scripts must import the split packages without caller-supplied PYTHONPATH."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parent.parent
BOOTSTRAPPED_SCRIPTS = (
    "scripts/gen_config_reference.py",
    "evals/collaboration_core_conformance.py",
    "evals/context_contract_eval.py",
    "evals/self_inspection_tool_eval.py",
    "evals/receipt_prompt_ab.py",
    "evals/selfnarrative_ab.py",
    "evals/usersim.py",
    "evals/usersim_pty.py",
)


def test_repo_scripts_import_split_packages_from_an_unrelated_cwd(tmp_path):
    expected = {
        "sliceagent": REPO / "src" / "sliceagent",
        "sliceagent_core": REPO / "packages" / "sliceagent-core" / "src" / "sliceagent_core",
        "sliceagent_cli": REPO / "packages" / "sliceagent-cli" / "src" / "sliceagent_cli",
    }
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    for relative in BOOTSTRAPPED_SCRIPTS:
        script = REPO / relative
        code = (
            "import pathlib, runpy; "
            f"runpy.run_path({str(script)!r}, run_name='bootstrap_probe'); "
            "import sliceagent, sliceagent_core, sliceagent_cli; "
            f"expected = { {name: str(path) for name, path in expected.items()}!r}; "
            "modules = {'sliceagent': sliceagent, 'sliceagent_core': sliceagent_core, "
            "'sliceagent_cli': sliceagent_cli}; "
            "assert all(pathlib.Path(modules[name].__file__).resolve().is_relative_to(pathlib.Path(root)) "
            "for name, root in expected.items())"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{relative}:\n{result.stdout}\n{result.stderr}"
