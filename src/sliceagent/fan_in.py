"""P5 compat shim — fan_in moved to sliceagent_core.fan_in (pure-stdlib turn-report
normalization; core slice_reducer depends on it). Deleted when callers migrate."""

from ._compat import alias_module as _alias_module

_alias_module(__name__, __spec__, "sliceagent_core.fan_in")
