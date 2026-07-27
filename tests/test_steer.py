"""Steer: user input typed mid-turn lands at step boundaries as a plain user message,
in the SAME conversation and the SAME turn — the in-flight model call is never aborted."""
from __future__ import annotations

import queue
from types import SimpleNamespace as NS

from sliceagent.events import SteerDelivered, TurnEnd
from sliceagent.hooks import Hooks
from sliceagent.loop import run_turn
from sliceagent.registry import ToolText


def _call(name: str, call_id: str, **args):
    return NS(name=name, id=call_id, args=args)


def _tool_response(call):
    return NS(content="", tool_calls=[call], finish_reason="tool_calls", usage={})


def _done_response(text="done"):
    return NS(content=text, tool_calls=[], finish_reason="stop", usage={})


class _ScriptLLM:
    def __init__(self, responses, on_call=None):
        self.responses = list(responses)
        self.seen = []
        self.on_call = on_call

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

    def run(self, name, args):
        return ToolText("observation")


def test_steer_lands_in_next_provider_call_after_tool_results():
    q: queue.Queue = queue.Queue()
    llm = _ScriptLLM(
        [_tool_response(_call("read_file", "c1", path="a.py")), _done_response()],
        on_call=lambda n: q.put("focus on the parser") if n == 1 else None,   # types during call 1
    )
    events = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "inspect"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=Hooks(), steer_queue=q,
    )
    assert outcome.stop_reason == "end_turn"
    assert len(llm.seen) == 2
    second = llm.seen[1]
    # sequence validity: the steer follows the tool result, as a plain user-role message
    assert second[-1] == {"role": "user", "content": "focus on the parser"}
    assert second[-2]["role"] == "tool" and second[-2]["tool_call_id"].startswith("c1")
    steers = [e for e in events if isinstance(e, SteerDelivered)]
    assert [e.content for e in steers] == ["focus on the parser"]


def test_last_second_steer_keeps_the_turn_alive():
    q: queue.Queue = queue.Queue()
    llm = _ScriptLLM(
        [_done_response("final answer"), _done_response("follow-up")],
        on_call=lambda n: q.put("wait, one more thing") if n == 1 else None,
    )
    events = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "do it"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=Hooks(), steer_queue=q,
    )
    assert outcome.stop_reason == "end_turn"
    assert len(llm.seen) == 2, "a steer arriving as the model finishes must force another step"
    second = llm.seen[1]
    assert second[-1] == {"role": "user", "content": "wait, one more thing"}
    assert second[-2]["role"] == "assistant" and second[-2]["content"] == "final answer"
    assert any(isinstance(e, TurnEnd) for e in events), "the turn still seals cleanly afterwards"


def test_no_steer_queue_means_clean_single_pass():
    llm = _ScriptLLM([_done_response()])
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "hi"}],
        llm=llm, tools=_Host(), dispatch=lambda _e: None, hooks=Hooks(),
    )
    assert outcome.stop_reason == "end_turn" and len(llm.seen) == 1
