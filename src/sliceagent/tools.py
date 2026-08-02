"""P4 compat shim — the concrete coding tools moved to ``sliceagent_cli.tools``.

Kept so existing CLI modules, integrations, tests, and downstream imports continue to
resolve during the modular migration. Deleted when callers move to the new name.
"""

from ._compat import alias_module as _alias_module

_alias_module(__name__, __spec__, "sliceagent_cli.tools")
