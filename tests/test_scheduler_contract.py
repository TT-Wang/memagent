"""Scheduler contract ownership and CLI injection boundary."""
from __future__ import annotations

import inspect

import sliceagent.loop as legacy_loop
import sliceagent_core.loop as core_loop
from sliceagent.scheduler import (
    DEFAULT_LIFECYCLE_ABSOLUTE,
    ORDERED_TOOL_SCHEDULER,
    ScheduledTool,
    run_ordered,
)
from sliceagent_core.interfaces import ToolScheduler
from sliceagent_core.scheduler_types import (
    DEFAULT_LIFECYCLE_ABSOLUTE as CORE_DEFAULT_LIFECYCLE_ABSOLUTE,
)
from sliceagent_core.scheduler_types import ScheduledTool as CoreScheduledTool


def test_scheduler_values_are_core_owned_and_concrete_adapter_implements_port() -> None:
    assert ScheduledTool is CoreScheduledTool
    assert DEFAULT_LIFECYCLE_ABSOLUTE == CORE_DEFAULT_LIFECYCLE_ABSOLUTE == 3600.0
    assert isinstance(ORDERED_TOOL_SCHEDULER, ToolScheduler)


def test_protocol_run_signature_tracks_concrete_run_ordered_signature() -> None:
    protocol_parameters = list(inspect.signature(ToolScheduler.run).parameters.values())[1:]
    concrete_parameters = list(inspect.signature(run_ordered).parameters.values())
    assert [parameter.name for parameter in protocol_parameters] == [
        parameter.name for parameter in concrete_parameters
    ]
    assert [parameter.default for parameter in protocol_parameters] == [
        parameter.default for parameter in concrete_parameters
    ]


def test_core_defaults_ordered_scheduler_and_port_stays_overridable() -> None:
    # Owner-directed reversal of the p3d placement (audit follow-up, 2026-08-02): the ordered
    # scheduler is turn semantics, so it lives in core and is the DEFAULT — core runs a turn
    # standalone. The ToolScheduler port remains overridable (None -> ORDERED at the body, so
    # the signature default is None, not the instance).
    assert inspect.signature(core_loop.run_turn).parameters["scheduler"].default is None
    assert inspect.signature(core_loop.run_tool_batch).parameters["scheduler"].default is None
    from sliceagent_core.scheduler import ORDERED_TOOL_SCHEDULER as core_ordered
    assert core_ordered is ORDERED_TOOL_SCHEDULER  # cli re-export is the same object
    # legacy facade is now a pure re-export of core — same functions, same defaults
    assert legacy_loop.run_turn is core_loop.run_turn
    assert legacy_loop.run_tool_batch is core_loop.run_tool_batch


def test_legacy_scheduler_monkeypatch_reaches_core() -> None:
    """The product alias stays the same module, so legacy monkeypatches reach core."""
    import sliceagent.scheduler as legacy
    import sliceagent_core.scheduler as core

    assert legacy is core
    sentinel = object()
    legacy._monkeypatch_probe = sentinel
    try:
        assert core._monkeypatch_probe is sentinel
    finally:
        del core._monkeypatch_probe
