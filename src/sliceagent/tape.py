"""P1 compat shim — the Session Tape lives in sliceagent_core.tape."""

from ._compat import alias_module as _alias_module

_alias_module(__name__, __spec__, "sliceagent_core.tape")
