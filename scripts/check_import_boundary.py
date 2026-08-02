#!/usr/bin/env python3
"""Enforce the package boundary: sliceagent_core must have ZERO imports from sliceagent_cli.

The bounded-slice engine (sliceagent-core) is the reusable runtime; the coding agent
(sliceagent-cli) is one host built on it. The dependency arrow points ONE way — cli -> core,
never core -> cli. This check makes that boundary a build error instead of a docstring.

Run: python scripts/check_import_boundary.py
Exit 0 = clean, Exit 1 = violations found.

Until the migration completes, this script is expected to report the remaining core->cli edges;
each migration phase burns some down until it reaches zero (the acceptance test for "done").
"""
from __future__ import annotations

import ast
import os
import sys

CORE_SRC = "packages/sliceagent-core/src/sliceagent_core"


def find_violations(core_src: str) -> list[str]:
    violations: list[str] = []
    for root, _, files in os.walk(core_src):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            except SyntaxError as exc:  # noqa: PERF203
                violations.append(f"{path}: could not parse ({exc})")
                continue
            for node in ast.walk(tree):
                mod = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    mod = node.module
                elif isinstance(node, ast.Import):
                    mod = ",".join(alias.name for alias in node.names)
                if mod and "sliceagent_cli" in mod:
                    violations.append(f"{path}:{getattr(node, 'lineno', '?')}: imports {mod}")
    return violations


def main() -> int:
    if not os.path.isdir(CORE_SRC):
        print(f"note: {CORE_SRC} does not exist yet (pre-migration); nothing to check.")
        return 0
    violations = find_violations(CORE_SRC)
    if violations:
        print(f"IMPORT BOUNDARY VIOLATIONS (core -> cli): {len(violations)} edge(s)")
        for v in violations:
            print(f"  {v}")
        return 1
    print("Import boundary clean: sliceagent_core has zero sliceagent_cli imports.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
