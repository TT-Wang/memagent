"""P3 compat shim — the generic resource-access model moved to core."""

from ._compat import alias_module as _alias_module

_alias_module(__name__, __spec__, "sliceagent_core.access")
