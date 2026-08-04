from __future__ import annotations

from types import SimpleNamespace as NS

from sliceagent.events import ToolResult
from sliceagent.hooks import Hooks
from sliceagent.loop import run_turn
from sliceagent.tools import LocalToolHost


def _call(name: str, call_id: str, **args):
    return NS(name=name, id=call_id, args=args)


def _response(*calls):
    return NS(content="", tool_calls=list(calls), finish_reason="tool_calls", usage={})


def _done():
    return NS(content="done", tool_calls=[], finish_reason="stop", usage={})


class _LLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []

    def complete(self, messages, _schemas):
        self.seen.append([dict(message) for message in messages])
        return self.responses.pop(0)


class _CountingHost(LocalToolHost):
    def __init__(self, *args, **kwargs):
        self.reads = 0
        super().__init__(*args, **kwargs)

    def _t_read_file(self, args):
        self.reads += 1
        return super()._t_read_file(args)


def _run(host, llm):
    events = []
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "inspect twice"}],
        llm=llm, tools=host, dispatch=events.append, hooks=Hooks(), max_steps=8,
    )
    # The last model call sees the complete growing trajectory exactly once and in provider order.
    contents = [
        message["content"] for message in llm.seen[-1]
        if message.get("role") == "tool"
    ]
    return result, events, contents


def test_t4_reexecutes_then_aliases_only_the_provider_view(monkeypatch, tmp_path):
    body = "\n".join(f"value-{i:04d}" for i in range(400))
    (tmp_path / "data.txt").write_text(body)
    monkeypatch.setenv("AGENT_EXPERIMENTAL_RESULT_ALIAS", "1")
    host = _CountingHost(root=str(tmp_path))
    llm = _LLM([
        _response(_call("read_file", "r1", path="data.txt")),
        _response(_call("read_file", "r2", path="data.txt")),
        _done(),
    ])

    result, events, messages = _run(host, llm)

    assert result.stop_reason == "end_turn"
    assert host.reads == 2, "T4 must re-observe; it is not an execution cache"
    assert "sliceagent_result_alias" not in messages[0]
    assert "sliceagent_result_alias" in messages[1]
    canonical = [event.output for event in events if isinstance(event, ToolResult)]
    assert canonical == [messages[0], messages[0]], \
        "durable/audit events must keep both complete physical observations"
    metrics = host.efficiency_metrics()
    assert metrics["result_repeat_count"] == 1
    assert metrics["result_alias_count"] == 1
    assert metrics["result_alias_saved_chars"] > 0
    blobs = list((tmp_path / ".sliceagent" / "blobs").glob("observation-*.txt"))
    assert len(blobs) == 1 and blobs[0].read_text() == messages[0]


def test_t4_flag_off_is_the_unchanged_full_result_path(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_EXPERIMENTAL_RESULT_ALIAS", raising=False)
    (tmp_path / "data.txt").write_text("alpha\nbeta\n")
    host = _CountingHost(root=str(tmp_path))
    llm = _LLM([
        _response(_call("read_file", "r1", path="data.txt")),
        _response(_call("read_file", "r2", path="data.txt")),
        _done(),
    ])

    _result, _events, messages = _run(host, llm)

    assert host.reads == 2 and messages[0] == messages[1]
    assert "sliceagent_result_alias" not in messages[1]
    assert host.efficiency_metrics()["result_repeat_count"] == 1
    assert host.efficiency_metrics()["result_alias_count"] == 0
    assert not (tmp_path / ".sliceagent" / "blobs").exists()


def test_t4_changed_result_is_never_aliased(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_EXPERIMENTAL_RESULT_ALIAS", "1")
    (tmp_path / "data.txt").write_text("old\n")
    host = _CountingHost(root=str(tmp_path))
    llm = _LLM([
        _response(_call("read_file", "r1", path="data.txt")),
        _response(_call("edit_file", "w1", path="data.txt", content="new\n")),
        _response(_call("read_file", "r2", path="data.txt")),
        _done(),
    ])

    _result, _events, messages = _run(host, llm)

    assert "old" in messages[0] and "new" in messages[2]
    assert "sliceagent_result_alias" not in messages[2]
    assert host.efficiency_metrics()["result_alias_count"] == 0


def test_t4_requires_a_recoverable_exact_source(monkeypatch):
    monkeypatch.setenv("AGENT_EXPERIMENTAL_RESULT_ALIAS", "1")

    class Host:
        def schemas(self): return []
        def accesses(self, _name, _args): return []
        def run(self, _name, _args): return "same exact body"
        def preserve_observation_result(self, _name, _args, _output): return None

    llm = _LLM([
        _response(_call("grep", "r1", pattern="x")),
        _response(_call("grep", "r2", pattern="x")),
        _done(),
    ])
    _result, _events, messages = _run(Host(), llm)

    assert messages[0] == messages[1] == "same exact body"
    assert "sliceagent_result_alias" not in messages[1]


def test_t4_does_not_treat_same_wave_dedup_as_cross_step_reobservation(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_EXPERIMENTAL_RESULT_ALIAS", "1")
    (tmp_path / "data.txt").write_text("one wave\n")
    host = _CountingHost(root=str(tmp_path))
    llm = _LLM([
        _response(
            _call("read_file", "r1", path="data.txt"),
            _call("read_file", "r2", path="data.txt"),
        ),
        _done(),
    ])

    _result, _events, messages = _run(host, llm)

    assert host.reads == 1, "the pre-existing same-wave physical dedup remains authoritative"
    assert len(messages) == 2 and messages[0] == messages[1]
    assert all("sliceagent_result_alias" not in message for message in messages)
