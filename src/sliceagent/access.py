"""P3 compat shim — the generic resource-access model moved to core."""
import sys as _sys

import sliceagent_core.access as _impl

_sys.modules[__name__] = _impl
