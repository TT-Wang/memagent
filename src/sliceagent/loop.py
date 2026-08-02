"""P5c compat shim — the loop facade moved to sliceagent_cli.loop_facade (it injects the
CLI scheduler = app composition). New callers: sliceagent_core.loop + supply a ToolScheduler."""
import sys as _sys
import sliceagent_cli.loop_facade as _impl
_sys.modules[__name__] = _impl
