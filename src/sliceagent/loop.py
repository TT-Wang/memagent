"""P5c compat shim — the loop facade moved to sliceagent_cli.loop_facade (it injects the
CLI scheduler = app composition). New callers: sliceagent_core.loop + supply a ToolScheduler."""

from ._compat import alias_module as _alias_module

_alias_module(__name__, __spec__, "sliceagent_cli.loop_facade")
