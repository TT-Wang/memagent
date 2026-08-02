"""sliceagent-core — the bounded-slice agent runtime.

Standalone: ``run_turn`` executes one bounded-slice turn with the ordered scheduler
defaulted — no CLI required. Every name advertised here is real and pinned by
``packages/sliceagent-core/tests/test_public_api.py``.

Entry points:
    from sliceagent_core import run_turn, run_tool_batch

Ports (Protocols a host may implement / override):
    from sliceagent_core import (
        LLMClient, ToolHost, ToolScheduler, Retriever, Memory,
        Registry, Safeguard, Oracle,
    )

Defaults shipped with the runtime:
    from sliceagent_core import NullMemory, ORDERED_TOOL_SCHEDULER

The one-way rule: this package NEVER imports sliceagent_cli. Enforced by
scripts/check_import_boundary.py in CI.
"""

__version__ = "0.1.0"

from .interfaces import (
    PeerResult,
    PeerWait,
    LLMClient,
    Memory,
    Oracle,
    Registry,
    Retriever,
    Safeguard,
    ToolHost,
    ToolScheduler,
)
from .loop import run_tool_batch, run_turn
from .memory_null import NullMemory
from .registry_types import ToolAdmission, ToolEntry, ToolText
from .scheduler import ORDERED_TOOL_SCHEDULER, OrderedToolScheduler
from .scheduler_types import DEFAULT_LIFECYCLE_ABSOLUTE, ScheduledTool

__all__ = [
    "__version__",
    # entry points
    "run_turn",
    "run_tool_batch",
    # ports
    "LLMClient",
    "ToolHost",
    "ToolScheduler",
    "Retriever",
    "Memory",
    "Registry",
    "Safeguard",
    "Oracle",
    "PeerWait",
    "PeerResult",
    # shipped defaults
    "NullMemory",
    "ORDERED_TOOL_SCHEDULER",
    "OrderedToolScheduler",
    # typed values carried by the contracts
    "ScheduledTool",
    "DEFAULT_LIFECYCLE_ABSOLUTE",
    "ToolAdmission",
    "ToolEntry",
    "ToolText",
]
