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


def test_core_requires_scheduler_while_legacy_cli_facade_injects_ordered_default() -> None:
    assert inspect.signature(core_loop.run_turn).parameters["scheduler"].default is inspect.Parameter.empty
    assert inspect.signature(core_loop.run_tool_batch).parameters["scheduler"].default is inspect.Parameter.empty
    assert inspect.signature(legacy_loop.run_turn).parameters["scheduler"].default is ORDERED_TOOL_SCHEDULER
    assert inspect.signature(legacy_loop.run_tool_batch).parameters["scheduler"].default is ORDERED_TOOL_SCHEDULER
