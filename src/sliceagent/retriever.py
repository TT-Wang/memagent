"""P1 compat shim — this module moved to sliceagent_core.retriever.
Kept so the 226 external references (tests/benchmarks/evals) + CLI modules keep working
during the migration; deleted when callers move to the new name (P5/P6)."""
import sys as _sys
import sliceagent_core.retriever as _impl
_sys.modules[__name__] = _impl
