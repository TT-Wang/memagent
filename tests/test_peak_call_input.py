"""peak_call_input / model_calls (owner's h2h fixes): the turn usage must carry the peak
SINGLE-CALL context window and the apple-to-apple model-call count, tracked once per
physical call across steps and closeouts — not just per-turn sums."""
from __future__ import annotations

from types import SimpleNamespace as NS

from sliceagent.events import TurnEnd
from sliceagent.hooks import Hooks
from sliceagent.loop import run_turn
from sliceagent.registry import ToolText


class _ScriptLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []

    def complete(self, messages, _schemas):
        self.seen.append([dict(message) for message in messages])
        return self.responses.pop(0)


class _Host:
    def schemas(self):
        return []

    def accesses(self, _name, _args):
        return []

    def run(self, name, args):
        return ToolText("observation")


def _done(text, usage):
    return NS(content=text, tool_calls=[], finish_reason="stop", usage=usage)


def _call(name, call_id, usage, **args):
    return NS(
        content="",
        tool_calls=[NS(name=name, id=call_id, args=args)],
        finish_reason="tool_calls",
        usage=usage,
    )


_SMALL = {"input_other": 1000, "input_cache_read": 2000, "input_cache_creation": 0, "output": 100}
_BIG = {"input_other": 4000, "input_cache_read": 12000, "input_cache_creation": 500, "output": 100}


def test_turn_usage_carries_peak_call_input_and_model_calls():
    llm = _ScriptLLM([
        _call("read_file", "c1", _SMALL, path="a.py"),   # call 1: 3,000 input
        _done("done", _BIG),                             # call 2: 16,500 input → the peak
    ])
    events = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "inspect"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=Hooks(),
    )
    assert outcome.stop_reason == "end_turn"
    turn_end = next(e for e in reversed(events) if isinstance(e, TurnEnd))
    usage = turn_end.usage
    assert usage["model_calls"] == 2, "one counter per physical model call"
    assert usage["peak_call_input"] == 16500, "the SINGLE biggest call window, not a turn sum"
    assert usage["peak_call_input"] < usage["prompt_tokens"], "peak is per-call, totals are larger"
    # the cache split rides the same record so the dollar row can price fresh vs cache-read
    assert usage["input_other"] == 5000
    assert usage["input_cache_read"] == 14000
    assert usage["input_cache_creation"] == 500


def test_single_call_turn_peak_equals_call_input():
    llm = _ScriptLLM([_done("done", _SMALL)])
    events = []
    run_turn(
        build_slice=lambda: [{"role": "user", "content": "hi"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=Hooks(),
    )
    usage = next(e for e in reversed(events) if isinstance(e, TurnEnd)).usage
    assert usage["model_calls"] == 1
    assert usage["peak_call_input"] == 3000
