"""P4 compat shim — the concrete coding tools moved to ``sliceagent_cli.tools``.

Kept so existing CLI modules, integrations, tests, and downstream imports continue to
resolve during the modular migration. Deleted when callers move to the new name.
"""
import sys as _sys

import sliceagent_cli.tools as _impl

_sys.modules[__name__] = _impl
