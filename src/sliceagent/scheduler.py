"""Compat shim — the ordered scheduler moved to sliceagent_core.scheduler (turn semantics
belongs to the runtime; the ToolScheduler port stays overridable, core defaults it)."""

from ._compat import alias_module as _alias_module

_alias_module(__name__, __spec__, "sliceagent_core.scheduler")
