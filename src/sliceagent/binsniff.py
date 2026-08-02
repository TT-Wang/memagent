"""P5b compat shim — moved to sliceagent_cli.binsniff. Old package is now a pure
compatibility umbrella (shims + the loop facade); deleted when external callers migrate."""
import sys as _sys
import sliceagent_cli.binsniff as _impl
_sys.modules[__name__] = _impl
