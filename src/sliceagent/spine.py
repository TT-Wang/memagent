"""P1 compat shim — the Session Spine renderer lives in sliceagent_core.spine.
Same pattern as the sibling shims: external callers (benchmarks/evals) keep the old
package name until the migration deletes this layer."""

from ._compat import alias_module as _alias_module

_alias_module(__name__, __spec__, "sliceagent_core.spine")
