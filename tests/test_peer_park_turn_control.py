"""The turn-ending peer park (task #104): a host tool can end a turn parked on a peer.

Control flow is recognised by TYPE, never by prose — a model cannot talk the kernel into
parking, and two parks in one batch is a typed conflict rather than a silent overwrite.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from sliceagent.execution import ToolPurity
from sliceagent.hooks import Hooks
from sliceagent.interfaces import PeerParkControl, PeerWait
from sliceagent.loop import run_turn
from sliceagent.registry import ToolEntry, ToolRegistry

PARK = PeerWait(correlation_id="ask-1", peer_id="sre", deadline_s=None)


def _host(*handlers):
    class Host:
        def __init__(self):
            self.registry = ToolRegistry()
            for index, handler in enumerate(handlers):
                name = f"tool_{index}"
                self.registry.register(ToolEntry(
                    name=name,
                    schema={"type": "function", "function": {
                        "name": name,
                        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                    }},
                    handler=handler, source="host", purity=ToolPurity.UNKNOWN, deduplicable=False,
                ))

        def schemas(self):
            return []

        def run(self, name, args):
            return self.registry.entry(name).handler(args)

        def read_text(self, path):
            raise FileNotFoundError(path)

        def accesses(self, name, args):
            return []

    return Host()


def _llm(calls):
    class LLM:
        def __init__(self):
            self.seen = 0

        def complete(self, messages, tools):
            self.seen += 1
            if self.seen <= len(calls):
                names = calls[self.seen - 1]
                return NS(
                    content="",
                    tool_calls=[NS(name=n, id=f"c{i}", args={}) for i, n in enumerate(names)],
                    finish_reason="tool_calls", usage={},
                )
            return NS(content="done", tool_calls=[], finish_reason="stop", usage={})

    return LLM()


def _run(host, llm):
    return run_turn(
        build_slice=lambda: [{"role": "user", "content": "ask the collaborator"}],
        llm=llm, tools=host, dispatch=lambda e: None, hooks=Hooks(),
    )


def test_a_host_tool_can_end_the_turn_parked_on_a_peer():
    llm = _llm([["tool_0"]])
    result = _run(_host(lambda args: PeerParkControl(PARK)), llm)
    assert result.stop_reason == "waiting_peer"
    assert result.peer_wait == PARK
    # The turn ENDED at the park: the model was not called again.
    assert llm.seen == 1


def test_prose_cannot_park_a_turn():
    """The kernel recognises a park by type. Text that merely claims one must not park."""
    llm = _llm([["tool_0"]])
    result = _run(_host(lambda args: "PeerParkControl(waiting_peer) — parking now"), llm)
    assert result.stop_reason != "waiting_peer"
    assert result.peer_wait is None


def test_two_parks_in_one_batch_is_a_typed_conflict():
    """Exclusivity: a turn can wait on exactly one collaborator.

    Silently keeping one park would leave the other correlation permanently unanswerable.
    """
    llm = _llm([["tool_0", "tool_1"]])
    result = _run(
        _host(
            lambda args: PeerParkControl(PARK),
            lambda args: PeerParkControl(
                PeerWait(correlation_id="ask-2", peer_id="other", deadline_s=None)
            ),
        ),
        llm,
    )
    assert result.stop_reason != "waiting_peer"
    assert result.peer_wait is None


def test_an_ordinary_turn_still_reports_no_park():
    llm = _llm([])
    result = _run(_host(lambda args: "ok"), llm)
    assert result.stop_reason == "end_turn"
    assert result.peer_wait is None


def test_a_finite_deadline_is_refused_at_the_boundary():
    """MVP scope: a bounded park needs platform capability we do not have."""
    with pytest.raises(ValueError):
        PeerParkControl(PeerWait(correlation_id="ask-3", peer_id="sre", deadline_s=30.0))
