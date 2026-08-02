"""sliceagent-core — the bounded-slice agent runtime.

Public API (wired as modules land in P1):
    from sliceagent_core import SliceAgent          # the engine
    from sliceagent_core import (                    # the injected contracts (ports)
        LLMClient, ToolHost, Retriever, Memory, NullMemory,
        PersistenceStore, ContextPolicy, EventSink,
    )

The one-way rule: this package NEVER imports sliceagent_cli. Enforced by
scripts/check_import_boundary.py in CI.
"""

__version__ = "0.1.0"

# TODO(P1): re-export the public surface once loop.py + interfaces.py land here, e.g.
#   from .loop import SliceAgent
#   from .interfaces import LLMClient, ToolHost, Retriever, Memory, PersistenceStore, ContextPolicy, EventSink
#   from .retriever import NullRetriever
from .memory_null import NullMemory  # the Memory contract's deterministic default

__all__ = ["__version__", "NullMemory"]
