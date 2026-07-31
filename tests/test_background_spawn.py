"""BACKGROUND (DETACHED) DELEGATION — spawn_agent with background=True.

Pins the detached-spawn contract: the call returns immediately with a typed ``running`` outcome,
the live matrix row is NEVER tombstoned by that in-band result, the finished report re-enters the
parent as a PeerMessage on the steer queue (mid-turn delivery at the next step boundary, or stashed
while idle and flushed into the next turn), and the terminal matrix settle carries the same typed
evidence/report labels a foreground ToolResult would.

No model, no pytest. Run: PYTHONPATH=src python tests/test_background_spawn.py
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent.agents import BUILTIN_AGENTS                     # noqa: E402
from sliceagent.background import BackgroundChildManager         # noqa: E402
from sliceagent.events import PeerMessageDelivered, TurnStarted  # noqa: E402
from sliceagent.execution import ToolStatus                      # noqa: E402
from sliceagent.hooks import Hooks                               # noqa: E402
from sliceagent.interfaces import PeerMessage                    # noqa: E402
from sliceagent.llm import AssistantMessage, ToolCall            # noqa: E402
from sliceagent.loop import _PEER_ENVELOPE_MARKER, run_tool_batch, run_turn  # noqa: E402
from sliceagent.memory import NullMemory                         # noqa: E402
from sliceagent.progress import TurnProgress                     # noqa: E402
from sliceagent.retriever import NullRetriever                   # noqa: E402
from sliceagent.scoped_spawn import ScopedSpawnHost              # noqa: E402

CHECKS = []
def check(fn):
    CHECKS.append(fn)
    return fn


class _ScriptedLLM:
    """One read step, then a final report — enough loop to prove the wiring."""

    model = "scripted"
    reasoning = ""

    def __init__(self, script=None):
        self.calls = 0
        self.messages_seen = []
        self._script = script

    def set_delta_sink(self, s):
        pass

    def set_transport_activity(self, s):
        pass

    def complete_with_control(self, messages, tools, *, should_cancel=None, transport_activity=None):
        return self.complete(messages, tools)

    def complete(self, messages, tools):
        self.calls += 1
        self.messages_seen.append(list(messages))
        if self._script is not None:
            return self._script(self.calls, messages, tools)
        if self.calls == 1:
            return AssistantMessage(content="", tool_calls=[
                ToolCall(id="c1", name="read_file", args={"path": "a.py"})],
                usage={"prompt_tokens": 100, "completion_tokens": 10})
        return AssistantMessage(content="REPORT: a.py defines f() returning 1.", tool_calls=[],
                                usage={"prompt_tokens": 120, "completion_tokens": 20})


def _workspace():
    import tempfile
    root = tempfile.mkdtemp(prefix="bg-spawn-")
    with open(os.path.join(root, "a.py"), "w", encoding="utf-8") as f:
        f.write("def f():\n    return 1\n")
    return root


def _host(root, *, manager=None, notify=None, llm=None, turn_id="t-1"):
    from sliceagent.tools import LocalToolHost
    return ScopedSpawnHost(
        LocalToolHost(root=root), llm=llm or _ScriptedLLM(), retriever=NullRetriever(),
        memory=NullMemory(), agents=BUILTIN_AGENTS, notify=notify,
        session_id="s-test", max_steps=5, turn_id_fn=lambda: turn_id,
        background=manager)


class _TC:
    def __init__(self, args, id="tc1"):
        self.id, self.name, self.args = id, "spawn_agent", args


def _wait_for(predicate, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ── spawn contract ─────────────────────────────────────────────────────────────────────────────

@check
def background_spawn_returns_immediately_with_a_typed_running_outcome():
    manager = BackgroundChildManager()
    host = _host(_workspace(), manager=manager)
    started = time.monotonic()
    out = host.run("spawn_agent", {"agent": "explorer", "task": "Read a.py.", "background": True})
    assert time.monotonic() - started < 5, "a detached spawn must not block on the child"
    assert "running in background" in str(out)
    effects = tuple(getattr(out, "effects", ()) or ())
    outcome = next(e for e in effects if e.kind == "child_outcome")
    assert outcome.payload["status"] == "running", outcome.payload
    assert outcome.payload["operational_status"] == "running"
    # the completion arrives as a PeerMessage (stashed: no turn is attached)
    assert _wait_for(lambda: manager._stash), "the settled child never delivered its report"
    peer = manager._stash[0]
    assert isinstance(peer, PeerMessage) and peer.wake == "none"
    assert "background report" in peer.content and "REPORT: a.py defines f()" in peer.content
    assert peer.peer_id.startswith("child-")
    assert _wait_for(lambda: manager._running == 0), "the manager still counts a finished child"


@check
def background_is_rejected_for_writable_kinds_and_without_a_manager():
    manager = BackgroundChildManager()
    host = _host(_workspace(), manager=manager)
    out = host.run("spawn_agent", {"agent": "general", "task": "x", "background": True})
    assert out.status == ToolStatus.STEERED and "read-only" in str(out), str(out)
    plain = _host(_workspace())   # no manager
    out = plain.run("spawn_agent", {"agent": "explorer", "task": "x", "background": True})
    assert out.status == ToolStatus.STEERED and "does not support background" in str(out), str(out)


@check
def detached_fan_out_is_bounded():
    gate = threading.Event()

    def script(calls, messages, tools):
        if calls == 1:
            return AssistantMessage(content="", tool_calls=[
                ToolCall(id="c1", name="read_file", args={"path": "a.py"})], usage={})
        gate.wait(30)   # the child never settles during the test
        return AssistantMessage(content="late", tool_calls=[], usage={})

    manager = BackgroundChildManager(max_running=1)
    host = _host(_workspace(), manager=manager, llm=_ScriptedLLM(script))
    try:
        first = host.run("spawn_agent", {"agent": "explorer", "task": "one", "background": True})
        assert "running in background" in str(first)
        assert _wait_for(lambda: manager._running == 1)
        second = host.run("spawn_agent", {"agent": "explorer", "task": "two", "background": True})
        assert second.status == ToolStatus.STEERED and "capacity" in str(second), str(second)
    finally:
        gate.set()   # release the child so its daemon thread can exit
    assert _wait_for(lambda: manager._running == 0)


# ── delivery into the parent turn ──────────────────────────────────────────────────────────────

@check
def a_settled_background_report_lands_at_the_next_step_boundary_as_typed_peer_input():
    manager = BackgroundChildManager()
    q = queue.Queue()
    manager.attach(q)
    host = _host(_workspace(), manager=manager)
    host.run("spawn_agent", {"agent": "explorer", "task": "Read a.py.", "background": True})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and q.empty():
        time.sleep(0.02)
    assert not q.empty(), "the completion never reached the attached steer queue"

    llm = _ScriptedLLM(lambda calls, messages, tools: AssistantMessage(
        content="done", tool_calls=[], usage={}))
    events = []
    from sliceagent.tools import LocalToolHost
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "original request"}],
        llm=llm, tools=LocalToolHost(root=_workspace()), dispatch=events.append, hooks=Hooks(),
        max_steps=3, steer_queue=q,
    )
    assert result.stop_reason == "end_turn"
    delivered = [e for e in events if isinstance(e, PeerMessageDelivered)]
    assert len(delivered) == 1, "the completion must be acked as typed peer input, not user prose"
    first_call = llm.messages_seen[0]
    peer_messages = [m for m in first_call if m.get("role") == "user"
                     and str(m.get("content", "")).startswith(_PEER_ENVELOPE_MARKER)]
    assert len(peer_messages) == 1, "the peer envelope must ride the very next provider call"
    assert "background report" in peer_messages[0]["content"]


@check
def an_idle_completion_is_stashed_and_flushed_into_the_next_turn():
    manager = BackgroundChildManager()
    host = _host(_workspace(), manager=manager)
    host.run("spawn_agent", {"agent": "explorer", "task": "Read a.py.", "background": True})
    assert _wait_for(lambda: manager._stash), "idle completion was not stashed"
    q = queue.Queue()
    manager.attach(q)
    flushed = q.get_nowait()
    assert isinstance(flushed, PeerMessage) and "background report" in flushed.content
    assert not manager._stash


@check
def reclaim_returns_undrained_completions_to_the_stash():
    manager = BackgroundChildManager()
    q = queue.Queue()
    manager.attach(q)
    peer = PeerMessage(message_id="m1", peer_id="child-1", content="[child 1 · report]")
    manager.deliver(peer)
    leftover = []
    while True:
        try:
            leftover.append(q.get_nowait())
        except queue.Empty:
            break
    manager.detach(reclaim=leftover)
    q2 = queue.Queue()
    manager.attach(q2)
    assert q2.get_nowait() is peer, "a completion the turn never drained must survive retirement"


# ── matrix truth ───────────────────────────────────────────────────────────────────────────────

@check
def the_in_band_running_result_never_tombstones_the_live_row():
    manager = BackgroundChildManager()
    progress = TurnProgress(await_commit=False)
    progress.reduce(TurnStarted(request="r", task_id="t", turn_id="t-1"))
    host = _host(_workspace(), manager=manager, notify=progress.subagent_activity)
    _, rows = run_tool_batch([_TC({"agent": "explorer", "task": "review", "background": True})],
                             host, progress.reduce, Hooks(), step=1, turn_id="t-1")
    assert "running in background" in rows[0]["output"]
    row = list(progress.snapshot().subagents)[-1]
    assert row.phase in {"queued", "starting", "running", "awaiting_model"}, row.phase
    assert row.evidence_status == "not_assessed", row.evidence_status
    # the out-of-band terminal settle carries the labels a foreground ToolResult would have
    assert _wait_for(lambda: list(progress.snapshot().subagents)[-1].phase == "report_ready")
    row = list(progress.snapshot().subagents)[-1]
    assert row.evidence_status == "content_retained", row.evidence_status
    assert row.report_completion == "complete", row.report_completion


if __name__ == "__main__":
    ok = 0
    for fn in CHECKS:
        try:
            fn()
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(CHECKS)} passed")
    sys.exit(0 if ok == len(CHECKS) else 1)
