"""Compatibility shim for CLI hook composition; core hooks live in sliceagent_core.hooks."""
import sys as _sys
import sliceagent_cli.hooks as _impl
_sys.modules[__name__] = _impl
