"""P5b compat shim — moved to sliceagent_cli.scoped_spawn. Old package is now a pure
compatibility umbrella (shims + the loop facade); deleted when external callers migrate."""

from ._compat import alias_module as _alias_module

_alias_module(__name__, __spec__, "sliceagent_cli.scoped_spawn")
