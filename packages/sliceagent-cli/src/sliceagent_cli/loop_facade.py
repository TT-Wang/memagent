"""Compatibility facade for the core loop with the CLI scheduler injected.

New callers should import :mod:`sliceagent_core.loop` and supply a ``ToolScheduler``.
The legacy ``sliceagent.loop`` surface (now a shim onto this module) keeps its existing call shape while the CLI
package is migrated, and supplies the concrete ordered scheduler at this boundary.
"""
from __future__ import annotations

from sliceagent_core import loop as _impl

from .scheduler import ORDERED_TOOL_SCHEDULER


def run_tool_batch(
    tool_calls,
    tools,
    dispatch,
    hooks,
    *,
    scheduler=ORDERED_TOOL_SCHEDULER,
    step: int = 0,
    turn_id: str = "",
    signal=None,
    call_namespace: str = "",
    steer_probe=None,
):
    return _impl.run_tool_batch(
        tool_calls,
        tools,
        dispatch,
        hooks,
        scheduler=scheduler,
        step=step,
        turn_id=turn_id,
        signal=signal,
        call_namespace=call_namespace,
        steer_probe=steer_probe,
    )


def run_turn(
    *,
    build_slice,
    llm,
    tools,
    dispatch,
    hooks=None,
    max_steps: int = 120,
    signal=None,
    checkpoint=None,
    consolidate=None,
    turn_id: str = "",
    call_namespace: str = "",
    transport_activity=None,
    allow_park_closeout: bool = True,
    steer_queue=None,
    followup_queue=None,
    scheduler=ORDERED_TOOL_SCHEDULER,
):
    return _impl.run_turn(
        build_slice=build_slice,
        llm=llm,
        tools=tools,
        scheduler=scheduler,
        dispatch=dispatch,
        hooks=hooks,
        max_steps=max_steps,
        signal=signal,
        checkpoint=checkpoint,
        consolidate=consolidate,
        turn_id=turn_id,
        call_namespace=call_namespace,
        transport_activity=transport_activity,
        allow_park_closeout=allow_park_closeout,
        steer_queue=steer_queue,
        followup_queue=followup_queue,
    )


def __getattr__(name: str):
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *dir(_impl)})
