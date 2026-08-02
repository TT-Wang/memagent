"""Compatibility shim for CLI hook composition; core hooks live in sliceagent_core.hooks."""

from ._compat import alias_module as _alias_module

_alias_module(__name__, __spec__, "sliceagent_cli.hooks")
