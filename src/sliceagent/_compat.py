"""Shared execution guard for the legacy ``sliceagent.<module>`` aliases."""
from __future__ import annotations

import importlib
import sys
from importlib.machinery import ModuleSpec
from types import ModuleType


def alias_module(module_name: str, module_spec: ModuleSpec | None, target: str) -> ModuleType:
    """Preserve import identity, but reject execution through a legacy module path."""
    if module_name == "__main__":
        legacy_name = module_spec.name if module_spec is not None else "sliceagent compatibility module"
        print(
            f"Compatibility module '{legacy_name}' moved to '{target}'; "
            f"invoke or import '{target}' instead.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    implementation = importlib.import_module(target)
    sys.modules[module_name] = implementation
    return implementation
