"""P3 compat shim — runtime budget guidance moved to core."""
import sys as _sys

import sliceagent_core.guidance as _impl

_sys.modules[__name__] = _impl
