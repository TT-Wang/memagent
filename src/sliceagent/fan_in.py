"""P5 compat shim — fan_in moved to sliceagent_core.fan_in (pure-stdlib turn-report
normalization; core slice_reducer depends on it). Deleted when callers migrate."""
import sys as _sys
import sliceagent_core.fan_in as _impl
_sys.modules[__name__] = _impl
