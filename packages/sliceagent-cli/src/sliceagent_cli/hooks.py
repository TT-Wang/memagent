"""CLI compatibility facade over the core hook contracts.

The reusable core requires an explicit :class:`Safeguard`. Legacy CLI callers used
``CatastrophicSafeguardHook()`` before that dependency became a port, so the CLI
boundary preserves that call shape by supplying its parser-backed implementation.
"""
from __future__ import annotations

from sliceagent_core.hooks import (
    ActiveWorkContinuationHook,
    BudgetHook,
    CatastrophicSafeguardHook as _CoreCatastrophicSafeguardHook,
    CompositeHooks,
    DeliverableCompletionHook,
    Hooks,
    OracleHook,
    PROCEED,
    ToolPreflight,
)
from sliceagent_core.interfaces import Safeguard

from .safeguards import CatastrophicSafeguard


class CatastrophicSafeguardHook(_CoreCatastrophicSafeguardHook):
    """Core hook with the CLI safeguard supplied for legacy arg-less callers."""

    def __init__(self, safeguard: Safeguard | None = None):
        super().__init__(safeguard if safeguard is not None else CatastrophicSafeguard())


__all__ = [
    "ActiveWorkContinuationHook",
    "BudgetHook",
    "CatastrophicSafeguardHook",
    "CompositeHooks",
    "DeliverableCompletionHook",
    "Hooks",
    "OracleHook",
    "PROCEED",
    "ToolPreflight",
]
