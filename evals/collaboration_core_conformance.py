"""RED, core-only conformance probes for native peer collaboration.

This is intentionally outside pytest's ``tests/`` collection.  It is an
acceptance executable for an interface that does not exist yet: current main
must exit 1 with C1/C2/C4 individually localized, while an implementation must
turn the same command green without changing the probes.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
import json
import os
import queue
import sys
from types import SimpleNamespace as NS
from typing import Any, Callable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


@dataclass(frozen=True)
class ProbeResult:
    contract: str
    passed: bool
    layer: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "passed": self.passed,
            "layer": self.layer,
            "detail": self.detail,
        }


def _require(module: str, name: str):
    value = getattr(importlib.import_module(module), name, None)
    if value is None:
        raise AssertionError(f"missing public core interface {module}.{name}")
    return value


def _construct(cls, **values):
    """Construct a public record while keeping constructor failures diagnostic."""
    try:
        return cls(**values)
    except TypeError as exc:
        raise AssertionError(
            f"{cls.__module__}.{cls.__name__} does not accept the contract fields "
            f"{tuple(values)}: {exc}"
        ) from exc


def probe_c1_waiting_peer() -> None:
    active_work = importlib.import_module("sliceagent.active_work")
    regions = importlib.import_module("sliceagent.regions")
    pfc = importlib.import_module("sliceagent.pfc")
    PeerWait = _require("sliceagent.interfaces", "PeerWait")

    assert "waiting_peer" in active_work.WORK_STATUSES, \
        "waiting_peer is absent from the durable Active Work lifecycle"
    assert "waiting_peer" in active_work.UNRESOLVED_STATUSES, \
        "waiting_peer must remain on the unresolved frontier"

    wait = _construct(
        PeerWait,
        correlation_id="review-42",
        peer_id="reviewer",
        deadline_s=30.0,
    )
    graph = active_work.WorkGraph().open_request(
        "peer-wait-request", "wait for an independent peer review",
        logical_id="peer-wait-logical",
    )
    try:
        parked = graph.seal_current(
            "waiting_peer",
            active_work.OutputRef("response", "progress-only"),
            peer_wait=wait,
            logical_id="peer-wait-logical",
        )
    except TypeError as exc:
        raise AssertionError(
            "WorkGraph.seal_current has no typed peer_wait correlation seam"
        ) from exc
    root = parked.unresolved_roots[-1] if parked.unresolved_roots else None
    assert root is not None and root.status == "waiting_peer", \
        "waiting_peer with progress output was delivered instead of parked"
    assert getattr(root, "peer_wait", None) == wait, \
        "the durable work item lost its typed peer correlation"

    state = pfc.Slice()
    state.active_work = parked
    state.edited_files = {"implementation.py"}
    state.since_edit = regions.STOP_NUDGE_AFTER + 2
    state.last_error = ""
    assert regions.render_convergence(state) == "", \
        "convergence pressure must be silent while the current request waits on a peer"

    resume = _require("sliceagent.active_work", "resume_waiting_peer")
    PeerResult = _require("sliceagent.interfaces", "PeerResult")
    wrong = _construct(
        PeerResult,
        correlation_id="review-wrong",
        peer_id="reviewer",
        status="ok",
        report="independently verified",
    )
    try:
        resume(parked, wrong, logical_id="peer-wait-logical")
    except (ValueError, active_work.ActiveWorkError):
        pass
    else:
        raise AssertionError("a mismatched peer result resumed the parked request")


class _ScriptLLM:
    def __init__(self, responses, on_call=None):
        self.responses = list(responses)
        self.on_call = on_call
        self.seen: list[list[dict]] = []

    def complete(self, messages, _schemas):
        self.seen.append([dict(message) for message in messages])
        if self.on_call is not None:
            self.on_call(len(self.seen))
        return self.responses.pop(0)


class _Host:
    def schemas(self):
        return []

    def accesses(self, _name, _args):
        return []

    def run(self, _name, _args):
        return _require("sliceagent.registry", "ToolText")("observation")


def _done(text: str):
    return NS(content=text, tool_calls=[], finish_reason="stop", usage={})


def probe_c2_typed_peer_steer() -> None:
    events_module = importlib.import_module("sliceagent.events")
    PeerMessage = _require("sliceagent.interfaces", "PeerMessage")
    PeerMessageDelivered = _require("sliceagent.events", "PeerMessageDelivered")
    Hooks = _require("sliceagent.hooks", "Hooks")
    run_turn = _require("sliceagent.loop", "run_turn")

    inbox: queue.Queue = queue.Queue()
    peer = _construct(
        PeerMessage,
        message_id="peer-message-7",
        peer_id="reviewer",
        content="reject: planted answer is wrong",
        correlation_id="review-42",
        wake="resume_wait",
    )
    llm = _ScriptLLM(
        [_done("candidate"), _done("revised")],
        on_call=lambda number: inbox.put(peer) if number == 1 else None,
    )
    emitted = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "produce and verify"}],
        llm=llm,
        tools=_Host(),
        dispatch=emitted.append,
        hooks=Hooks(),
        steer_queue=inbox,
    )
    assert outcome.stop_reason == "end_turn" and len(llm.seen) == 2, \
        "an admitted peer wake must keep the same running turn alive"
    typed = [event for event in emitted if isinstance(event, PeerMessageDelivered)]
    assert len(typed) == 1, "peer admission did not emit exactly one typed delivery event"
    assert not isinstance(typed[0], events_module.SteerDelivered), \
        "peer delivery is not a user SteerDelivered event"
    assert getattr(typed[0], "correlation_id", "") == "review-42", \
        "peer delivery lost correlation identity"
    assert getattr(typed[0], "peer_id", "") == "reviewer", \
        "peer delivery lost sender identity"
    delivered = llm.seen[1][-1]
    assert delivered.get("role") == "user", "peer envelope must use a provider-supported input role"
    assert delivered.get("content") != peer.content, \
        "peer input reached the model as unattributed plain user text"
    assert "reviewer" in str(delivered.get("content")) and "review-42" in str(delivered.get("content")), \
        "the bounded provider envelope omitted peer attribution or correlation"


def probe_c4_correlated_delegation_return() -> None:
    PeerDelegation = _require("sliceagent.interfaces", "PeerDelegation")
    PeerResult = _require("sliceagent.interfaces", "PeerResult")
    correlate = _require("sliceagent.interfaces", "correlate_peer_result")

    delegation = _construct(
        PeerDelegation,
        correlation_id="delegate-9",
        peer_id="worker",
        task="inspect shard B",
        deadline_s=20.0,
    )
    wrong = _construct(
        PeerResult,
        correlation_id="delegate-other",
        peer_id="worker",
        status="ok",
        report="unrelated result",
    )
    matching = _construct(
        PeerResult,
        correlation_id="delegate-9",
        peer_id="worker",
        status="ok",
        report="shard B verified",
    )
    assert correlate(delegation, wrong) is None, \
        "a delegation accepted a result with the wrong correlation ID"
    accepted = correlate(delegation, matching)
    assert accepted is not None, "a matching typed peer result was not accepted"
    assert getattr(accepted, "correlation_id", "") == "delegate-9", \
        "the terminal peer outcome lost correlation identity"
    assert getattr(accepted, "status", "") == "ok", \
        "the terminal peer outcome lost typed result status"
    assert getattr(accepted, "report", "") == "shard B verified", \
        "the correlated report is not addressable without parsing prose"

    expired = _construct(
        PeerDelegation,
        correlation_id="delegate-expired",
        peer_id="worker",
        task="late work",
        deadline_s=0.0,
    )
    late = _construct(
        PeerResult,
        correlation_id="delegate-expired",
        peer_id="worker",
        status="ok",
        report="too late",
    )
    assert correlate(expired, late, elapsed_s=0.001) is None, \
        "a result resurrected an expired delegation"


PROBES: tuple[tuple[str, Callable[[], None]], ...] = (
    ("C1_waiting_peer", probe_c1_waiting_peer),
    ("C2_typed_peer_steer", probe_c2_typed_peer_steer),
    ("C4_correlated_delegation_return", probe_c4_correlated_delegation_return),
)


def run() -> list[ProbeResult]:
    results = []
    for contract, probe in PROBES:
        try:
            probe()
        except Exception as exc:  # noqa: BLE001 - each deficit must remain independently localized
            results.append(ProbeResult(contract, False, "core", f"{type(exc).__name__}: {exc}"))
        else:
            results.append(ProbeResult(contract, True, "core", "contract satisfied"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="emit one JSON object containing all probe results")
    args = parser.parse_args()
    results = run()
    payload = {
        "schema_version": 1,
        "passed": all(item.passed for item in results),
        "summary": {
            "passed": sum(item.passed for item in results),
            "failed": sum(not item.passed for item in results),
            "total": len(results),
        },
        "results": [item.to_dict() for item in results],
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        for item in results:
            print(json.dumps(item.to_dict(), sort_keys=True))
        print(json.dumps({"summary": payload["summary"], "passed": payload["passed"]}, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
