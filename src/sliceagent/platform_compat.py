"""P1 compat shim — this module moved to sliceagent_core.platform_compat.
Kept so the 226 external references (tests/benchmarks/evals) + CLI modules keep working
during the migration; deleted when callers move to the new name (P5/P6)."""

from ._compat import alias_module as _alias_module

_alias_module(__name__, __spec__, "sliceagent_core.platform_compat")
