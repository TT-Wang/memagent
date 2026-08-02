"""Delegation lifecycle semantics share one classification source."""

from types import SimpleNamespace

import sliceagent.execution as execution
import sliceagent.loop as loop
from sliceagent.access import ReadAllAccess
from sliceagent.events import ToolResult
from sliceagent.hooks import Hooks
from sliceagent.receipts import compact_receipt_projection


def test_alias_delegation_keeps_leases_reconciliation_and_receipt_accounting(monkeypatch) -> None:
    alias = "delegate_task"
    monkeypatch.setattr(
        execution,
        "DELEGATION_TOOL_NAMES",
        frozenset({*execution.DELEGATION_TOOL_NAMES, alias}),
    )
    captured = {}

    class Host:
        def schemas(self):
            return [{
                "type": "function",
                "function": {
                    "name": alias,
                    "description": "fixture",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]

        def accesses(self, _name, _args):
            return [ReadAllAccess()]

        def run(self, name, args):
            captured["name"] = name
            captured["args"] = args
            return "child report"

    class Scheduler:
        def run(self, tasks, *, on_outcomes, **_kwargs):
            task = tasks[0]
            captured["scheduled"] = task
            on_outcomes([task.run()])
            return []

    events = []
    failures, results = loop.run_tool_batch(
        [SimpleNamespace(id="alias-1", name=alias, args={"agent": "explorer"})],
        Host(),
        events.append,
        Hooks(),
        scheduler=Scheduler(),
        step=1,
    )

    task = captured["scheduled"]
    assert task.timeout_safe is False
    assert task.activity is not None and task.request_cancel is not None and task.on_queued is not None
    assert execution.CHILD_ACTIVITY_ARG in captured["args"]
    assert execution.CHILD_CANCEL_SIGNAL_ARG in captured["args"]
    assert execution.reconciliation_targets(alias, {"agent": "explorer"}) == ()
    assert any(isinstance(event, ToolResult) for event in events)
    assert failures == 0 and results[0]["status"] == "succeeded"

    compact = compact_receipt_projection({
        "turn_status": "end_turn",
        "operations": [{
            "name": alias,
            "requested": True,
            "execution_started": True,
            "settled": True,
            "outcome_status": "succeeded",
            "artifact_refs": ["child-a"],
        }],
    })
    assert compact["agents"]["requested"] == 1
    assert compact["agents"]["child_artifacts"] == 1
