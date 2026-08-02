"""P-memory compat shim — moved to sliceagent_cli.hippocampus (Memem stack = CLI's default
Memory block, injected into core via the Memory contract). Deleted when callers migrate."""

from ._compat import alias_module as _alias_module

_alias_module(__name__, __spec__, "sliceagent_cli.hippocampus")
