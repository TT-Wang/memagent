"""P-memory compat shim — moved to sliceagent_cli.memory (Memem stack = CLI's default
Memory block, injected into core via the Memory contract). Deleted when callers migrate."""
import sys as _sys
import sliceagent_cli.memory as _impl
_sys.modules[__name__] = _impl
