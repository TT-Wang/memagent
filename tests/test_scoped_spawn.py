"""SCOPED SPAWN — the subagent-as-scoped-turn core (docs/SUBAGENT-SCOPED-TURN.md).

Ports the load-bearing guarantees of the old subagent suite onto the new core:
MATRIX TRUTH (typed SubagentProgress with stable identities + monotonic sequence), READONLY CHILD
(a read-only kind provably cannot reach a mutating tool, on every dispatch protocol), DEPTH-1
(no child surface ever contains a spawn tool), the WORK BINDING (acceptance contract into the brief,
stale ids rejected loudly), and the DUMB SEAL (one redacted JSON record; sub-N.md re-readable).

No model, no pytest. Run: PYTHONPATH=src python tests/test_scoped_spawn.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent.access import AllAccess, ReadAllAccess            # noqa: E402
from sliceagent.agents import BUILTIN_AGENTS                       # noqa: E402
from sliceagent.events import SubagentProgress                     # noqa: E402
from sliceagent.execution import ToolStatus                        # noqa: E402
from sliceagent.hooks import Hooks                                 # noqa: E402
from sliceagent.llm import AssistantMessage, ToolCall              # noqa: E402
from sliceagent.loop import run_tool_batch                         # noqa: E402
from sliceagent.memory import NullMemory                           # noqa: E402
from sliceagent.retriever import NullRetriever                     # noqa: E402
from sliceagent.scoped_agent import ScopedSurface, allowed_for     # noqa: E402
from sliceagent.scoped_spawn import ScopedSpawnHost                # noqa: E402
from sliceagent.tools import LocalToolHost                         # noqa: E402

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
        self._script = script

    def set_delta_sink(self, s):
        pass

    def set_transport_activity(self, s):
        pass

    def complete_with_control(self, messages, tools, *, should_cancel=None, transport_activity=None):
        return self.complete(messages, tools)

    def complete(self, messages, tools):
        self.calls += 1
        if self._script is not None:
            return self._script(self.calls, messages, tools)
        if self.calls == 1:
            return AssistantMessage(content="", tool_calls=[
                ToolCall(id="c1", name="read_file", args={"path": "a.py"})],
                usage={"prompt_tokens": 100, "completion_tokens": 10})
        return AssistantMessage(content="REPORT: a.py defines f() returning 1.", tool_calls=[],
                                usage={"prompt_tokens": 120, "completion_tokens": 20})


def _workspace():
    root = tempfile.mkdtemp(prefix="scoped-spawn-")
    with open(os.path.join(root, "a.py"), "w", encoding="utf-8") as f:
        f.write("def f():\n    return 1\n")
    return root


def _host(root, *, llm=None, notify=None, memory=None, work_provider=None, intent_provider=None,
          session_id="s-test", turn_id="t-1"):
    return ScopedSpawnHost(
        LocalToolHost(root=root), llm=llm or _ScriptedLLM(), retriever=NullRetriever(),
        memory=memory or NullMemory(), agents=BUILTIN_AGENTS, notify=notify,
        session_id=session_id, max_steps=5, turn_id_fn=lambda: turn_id,
        work_provider=work_provider, intent_provider=intent_provider)


class _TC:
    def __init__(self, args, id="tc1"):
        self.id, self.name, self.args = id, "spawn_agent", args


# ── schema + scheduling contract ─────────────────────────────────────────────────────────────────

@check
def spawn_schema_is_stable_and_kinds_live():
    host = _host(_workspace())
    fn = next(s["function"] for s in host.schemas() if s["function"]["name"] == "spawn_agent")
    assert set(fn["parameters"]["properties"]) == {"agent", "task", "work_item_id", "scope",
                                                   "exclusions"}
    assert fn["parameters"]["required"] == ["agent", "task"]
    assert set(fn["parameters"]["properties"]["agent"]["enum"]) >= {"explorer", "general",
                                                                    "debugger", "reviewer"}


@check
def accesses_classify_read_only_vs_writable_kinds():
    host = _host(_workspace())
    ro = host.accesses("spawn_agent", {"agent": "explorer"})
    assert len(ro) == 1 and isinstance(ro[0], ReadAllAccess), ro
    rw = host.accesses("spawn_agent", {"agent": "general"})
    assert len(rw) == 1 and isinstance(rw[0], AllAccess), rw
    unknown = host.accesses("spawn_agent", {"agent": "nope"})
    assert isinstance(unknown[0], AllAccess), "an unknown kind must be pessimistically exclusive"


@check
def depth_one_no_child_surface_contains_spawn():
    host = _host(_workspace())
    for kind, spec in BUILTIN_AGENTS.items():
        names = allowed_for(spec, host)
        assert "spawn_agent" not in names, f"depth leak via kind {kind}"


@check
def child_barred_tools_and_private_mounts_are_gated():
    """Parity with the old child-surface taxonomy: ask_user/update_work/change_workspace/
    search_history are steered quietly; disallowed-but-real tools fail LOUD; parent-private mounts
    are unreachable on reads — in any path spelling."""
    root = _workspace()
    inner = LocalToolHost(root=root)
    for kind, spec in BUILTIN_AGENTS.items():
        names = allowed_for(spec, inner)
        for barred in ("ask_user", "update_work", "change_workspace", "search_history"):
            assert barred not in names, f"{barred} leaked into kind {kind}"
    # Privacy is ROUTING truth, not spelling: mount the artifact FS (as production does), then every
    # spelling of the mounted namespace is parent-private, while a bare name stays a workspace path.
    from types import SimpleNamespace
    from sliceagent.runtime_persistence import CoreArtifactFS

    class _Store:
        artifact = SimpleNamespace(
            id="turn-1", kind="turn", title="t", task_id="task-1", status="completed",
            timestamp="2026-01-01T00:00:00Z", brief={}, summary="s", structured_body={}, refs=())
        def list_all(self):
            return [self.artifact]
        def get(self, artifact_id):
            return self.artifact if artifact_id == "turn-1" else None

    inner._artifacts = CoreArtifactFS(_Store())
    surface = ScopedSurface(inner, allowed_for(BUILTIN_AGENTS["explorer"], inner))
    ask = surface.run("ask_user", {"question": "?"})
    assert ask.status == ToolStatus.CANCELLED and "assumption" in str(ask)
    work = surface.run("update_work", {"items": []})
    assert work.status == ToolStatus.CANCELLED
    write = surface.run("edit_file", {"path": "a.py", "content": "x"})
    assert write.status == ToolStatus.FAILED, "a write attempt must be LOUD, not a quiet steer"
    for spelling in ("artifacts/turn-1.md", os.path.join(root, "artifacts", "turn-1.md"),
                     "./artifacts/../artifacts/turn-1.md", "@sliceagent/index.md"):
        blocked = surface.run("read_file", {"path": spelling})
        assert getattr(blocked, "status", None) == ToolStatus.CANCELLED, (spelling, blocked)
        assert "private namespace" in str(blocked), (spelling, blocked)
    ok_read = surface.run("read_file", {"path": "a.py"})
    assert "def f" in str(ok_read), "ordinary workspace reads must pass through"


# ── the real dispatch path ───────────────────────────────────────────────────────────────────────

@check
def spawn_through_run_tool_batch_returns_child_report():
    host = _host(_workspace())
    _, results = run_tool_batch(
        [_TC({"agent": "explorer", "task": "Read a.py and report what f returns."})],
        host, lambda e: None, Hooks(), step=1, turn_id="t-1")
    text = str(results[0].get("output") if isinstance(results[0], dict) else results[0])
    assert "child 1 · explorer · ok · 2 steps" in text, text
    assert "REPORT: a.py defines f()" in text, text


@check
def matrix_truth_progress_phases_and_identities():
    seen = []
    host = _host(_workspace(), notify=seen.append)
    run_tool_batch([_TC({"agent": "explorer", "task": "Read a.py."})],
                   host, lambda e: None, Hooks(), step=1, turn_id="t-1")
    ups = [u for u in seen if isinstance(u, SubagentProgress)]
    assert ups, "no progress reached the matrix"
    phases = [u.phase for u in ups]
    assert phases[0] == "starting" and phases[-1] == "settling", phases
    assert "running_tool" in phases, phases
    seqs = [u.sequence for u in ups]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "sequence must be monotonic"
    assert all(u.parent_turn_id == "t-1" and u.agent_id for u in ups)
    assert all(u.kind == "explorer" for u in ups)


@check
def readonly_child_cannot_reach_a_mutating_tool_on_any_protocol():
    """A write attempt from a read-only child is a LOUD failure (capability escalation, not benign
    steering) and provably never reaches the inner host — on every dispatch protocol."""
    root = _workspace()
    inner = LocalToolHost(root=root)
    surface = ScopedSurface(inner, allowed_for(BUILTIN_AGENTS["explorer"], inner))
    victim = os.path.join(root, "a.py")
    before = open(victim, encoding="utf-8").read()
    for attempt in (
        lambda: surface.run("edit_file", {"path": "a.py", "content": "PWNED"}),
        lambda: surface.run_preflighted("str_replace", {"path": "a.py", "old": "1", "new": "2"},
                                        None),
    ):
        out = attempt()
        assert getattr(out, "status", None) == ToolStatus.FAILED, out
    admission, steer = surface.preflight_run("run_command", {"command": "rm -rf ."})
    assert admission is None and getattr(steer, "status", None) == ToolStatus.FAILED
    assert open(victim, encoding="utf-8").read() == before, "the workspace was mutated!"
    assert "edit_file" not in {s["function"]["name"] for s in surface.schemas()}


@check
def scripted_child_write_attempt_is_steered_through_the_live_loop():
    """Loop-driving: a read-only child that CALLS edit_file gets ↷ steered, then reports."""
    def script(call, messages, tools):
        names = {t["function"]["name"] for t in tools}
        assert "edit_file" not in names, "a read-only child must not even SEE edit_file"
        if call == 1:
            return AssistantMessage(content="", tool_calls=[
                ToolCall(id="x1", name="edit_file", args={"path": "a.py", "content": "PWNED"})],
                usage={})
        return AssistantMessage(content="Could not edit (read-only); a.py has f() -> 1.",
                                tool_calls=[], usage={})
    root = _workspace()
    host = _host(root, llm=_ScriptedLLM(script))
    _, results = run_tool_batch([_TC({"agent": "explorer", "task": "Fix a.py."})],
                                host, lambda e: None, Hooks(), step=1, turn_id="t-1")
    text = str(results[0].get("output") if isinstance(results[0], dict) else results[0])
    assert "child 1 · explorer · ok" in text, text
    assert open(os.path.join(root, "a.py"), encoding="utf-8").read() == "def f():\n    return 1\n"


@check
def cancelled_child_is_a_quiet_typed_slot():
    lease = threading.Event()
    lease.set()                                   # cancelled before the child starts
    from sliceagent.execution import CHILD_CANCEL_SIGNAL_ARG
    host = _host(_workspace())
    out = host.run("spawn_agent", {"agent": "explorer", "task": "Read a.py.",
                                   CHILD_CANCEL_SIGNAL_ARG: lease})
    assert getattr(out, "status", None) == ToolStatus.CANCELLED, out
    assert "cancelled" in str(out)


# ── brief assembly: constraints + the work binding ───────────────────────────────────────────────

@check
def standing_constraints_are_forwarded_verbatim_never_the_request():
    class _Intent:
        current_request = "orchestrate the big review"
        def resident_entries(self):
            return [{"verbatim_clause": "never modify config.py", "status": "active"},
                    {"verbatim_clause": "orchestrate the big review", "status": "active"},
                    {"verbatim_clause": "stale one", "status": "dropped"}]
    captured = {}
    def script(call, messages, tools):
        captured["seed"] = "\n".join(str(m.get("content", "")) for m in messages)
        return AssistantMessage(content="done", tool_calls=[], usage={})
    host = _host(_workspace(), llm=_ScriptedLLM(script), intent_provider=lambda task: _Intent())
    host.run("spawn_agent", {"agent": "explorer", "task": "Audit a.py."})
    assert "never modify config.py" in captured["seed"]
    assert "orchestrate the big review" not in captured["seed"], \
        "the parent's current request must not become a child constraint"
    assert "stale one" not in captured["seed"]


@check
def work_binding_injects_the_acceptance_contract_and_rejects_stale_ids():
    class _Item:
        """Duck-typed WorkItem view: the host reads only id/description/done_when/verify."""
        id = "w1"
        description = "migrate the cache"
        done_when = "all namespaces invalidate"
        verify = ("pytest -q tests/x.py",)
    class _G:
        def __init__(self, item):
            self._item = item
        def get(self, item_id):
            return self._item if item_id == self._item.id else None
    item = _Item()
    captured = {}
    def script(call, messages, tools):
        captured["seed"] = "\n".join(str(m.get("content", "")) for m in messages)
        return AssistantMessage(content="done", tool_calls=[], usage={})
    host = _host(_workspace(), llm=_ScriptedLLM(script), work_provider=lambda: _G(item))
    out = host.run("spawn_agent", {"agent": "explorer", "task": "Do w1.", "work_item_id": "w1"})
    assert "child 1" in str(out)
    assert "bound work item w1" in captured["seed"]
    assert "all namespaces invalidate" in captured["seed"]
    assert "pytest -q tests/x.py" in captured["seed"]
    stale = host.run("spawn_agent", {"agent": "explorer", "task": "x", "work_item_id": "w404"})
    assert stale.status == ToolStatus.STEERED and "does not name a live ACTIVE WORK item" in str(stale)


# ── the dumb seal ────────────────────────────────────────────────────────────────────────────────

@check
def seal_appends_one_redacted_record_and_renders_sub_n():
    vault = tempfile.mkdtemp(prefix="scoped-vault-")
    prior = os.environ.get("SLICEAGENT_VAULT")
    os.environ["SLICEAGENT_VAULT"] = vault
    try:
        from sliceagent.memory import LocalMemory
        from sliceagent.hippocampus import SubagentFS, render_artifact
        memory = LocalMemory(prefer_memem=False)
        host = _host(_workspace(), memory=memory, session_id="seal-test")
        out = host.run("spawn_agent", {"agent": "explorer", "task": "Read a.py and report."})
        assert 'read_file("subagents/sub-1.md")' in str(out), out
        arts = memory.read_subagent_artifacts("seal-test")
        assert len(arts) == 1 and arts[0]["id"] == "sub-1"
        md = render_artifact(arts[0])
        assert "explorer" in md and "REPORT: a.py defines f()" in md
        assert "## brief (verbatim task this agent was given)" in md
        fs = SubagentFS(memory, "seal-test")
        assert "sub-1.md" in "".join(fs._names(fs._arts()))
        out2 = host.run("spawn_agent", {"agent": "explorer", "task": "Again."})
        assert 'subagents/sub-2.md' in str(out2), "ordinals must advance"
    finally:
        if prior is None:
            os.environ.pop("SLICEAGENT_VAULT", None)
        else:
            os.environ["SLICEAGENT_VAULT"] = prior
        import shutil
        shutil.rmtree(vault, ignore_errors=True)


@check
def seal_failure_never_gates_the_inline_report():
    class _BrokenMemory(NullMemory):
        def append_subagent_artifact(self, session_id, artifact):
            raise RuntimeError("vault on fire")
    out = _host(_workspace(), memory=_BrokenMemory()).run(
        "spawn_agent", {"agent": "explorer", "task": "Read a.py."})
    assert "REPORT: a.py defines f()" in str(out), "the report must survive a seal failure"
    assert "subagents/" not in str(out), "no locator may be advertised for a failed seal"


# ── typed error edges ────────────────────────────────────────────────────────────────────────────

@check
def indeterminate_child_stays_distinct_from_failed():
    """An unconfirmed-close/timeout child has an UNKNOWN physical state; collapsing it into
    'failed' erases truth. The status survives the mapping and the parent envelope."""
    from sliceagent.scoped_agent import _STOP_TO_STATUS
    assert _STOP_TO_STATUS.get("indeterminate") == "indeterminate"

    def script(call, messages, tools):
        raise TimeoutError("simulated transport stall")
    host = _host(_workspace(), llm=_ScriptedLLM(script))
    out = host.run("spawn_agent", {"agent": "explorer", "task": "Read a.py."})
    # a crashing transport parks as error → failed (not indeterminate); assert the DISTINCTION:
    assert "failed" in str(out) or "error" in str(out), out
    # and the envelope for a true indeterminate result carries the unknown-state warning
    from sliceagent.scoped_agent import ScopedResult
    fake = ScopedResult(report="half a report", status="indeterminate",
                        stop_reason="indeterminate", steps=2)
    host2 = _host(_workspace())
    header = "[child 1 · explorer · indeterminate · 2 steps]"
    # drive the envelope branch directly through _spawn's formatting contract:
    import sliceagent.scoped_spawn as sp
    orig = sp.run_scoped_agent
    sp.run_scoped_agent = lambda *a, **k: fake
    try:
        out2 = host2.run("spawn_agent", {"agent": "explorer", "task": "x"})
    finally:
        sp.run_scoped_agent = orig
    assert header in str(out2) and "UNKNOWN" in str(out2), out2
    assert out2.ok is False
    assert getattr(out2, "status", None) == ToolStatus.INDETERMINATE


@check
def matrix_updates_live_through_the_real_progress_reducer():
    """THE FREEZE REGRESSION: the TUI matrix drops any child update whose parent_turn_id differs
    from TurnStarted.turn_id (stale-callback protection). The host must therefore emit the
    PRESENTATION turn id — a task id freezes every row at its 'starting' placeholder."""
    from sliceagent.events import TurnStarted
    from sliceagent.progress import TurnProgress

    from sliceagent.events import ToolResult as _TR

    def _drive(host_turn_id):
        """Return the child row as it stood JUST BEFORE the terminal ToolResult — the live window
        where the freeze is observable (the authoritative ToolResult retires the row either way)."""
        progress = TurnProgress(await_commit=False)
        progress.reduce(TurnStarted(request="r", task_id="t-1", turn_id="turn-ART-7"))
        pre_terminal = {}

        def dispatch(event):
            if isinstance(event, _TR) and event.name == "spawn_agent":
                rows = list(progress.snapshot().subagents)
                if rows:
                    pre_terminal["row"] = rows[-1]
            progress.reduce(event)

        host = _host(_workspace(), notify=progress.subagent_activity, turn_id=host_turn_id)
        run_tool_batch([_TC({"agent": "explorer", "task": "Read a.py."})],
                       host, dispatch, Hooks(), step=1, turn_id="turn-ART-7")
        assert "row" in pre_terminal, "the child row must exist before its terminal ToolResult"
        return pre_terminal["row"]

    live = _drive("turn-ART-7")                       # matches TurnStarted.turn_id
    assert live.sequence > 0, f"live updates were rejected — matrix frozen (seq={live.sequence})"
    assert live.phase not in ("queued", "starting"), \
        f"row never advanced past the placeholder: {live.phase}"

    frozen = _drive("t-1")                            # the BUG: task id instead of turn id
    assert frozen.sequence <= 0 and frozen.phase in ("queued", "starting"), (
        "negative control failed — a mismatched parent_turn_id should freeze the row; "
        "if this asserts, the reducer's stale gate changed and the host wire must be re-audited")


@check
def fanout_returns_ordered_typed_slots_and_survives_crash_and_cancel():
    """The bounded parallel runner's contract: results in submission order, a crashing child is a
    typed failed slot (never a fan-out crash), and a pre-set cancel yields cancelled slots."""
    import sliceagent.scoped_agent as sa
    from sliceagent.fanout import FanoutTask, run_fanout
    from sliceagent.scoped_agent import ScopedResult

    root = _workspace()
    tools = LocalToolHost(root=root)

    def fake_run(task, **kw):
        if "boom" in task:
            raise RuntimeError("child exploded")
        if kw.get("signal") is not None and kw["signal"].is_set():
            return ScopedResult(status="cancelled", stop_reason="aborted")
        return ScopedResult(report=f"done:{task}", status="ok", stop_reason="end_turn", steps=1)

    orig = sa.run_scoped_agent
    import sliceagent.fanout as fo
    orig_fo = fo.run_scoped_agent
    fo.run_scoped_agent = fake_run
    try:
        outs = run_fanout([FanoutTask(task="alpha"), FanoutTask(task="boom"),
                           FanoutTask(task="gamma", kind="reviewer")],
                          tools=tools, llm=_ScriptedLLM(), retriever=NullRetriever(),
                          memory=NullMemory(), max_workers=3)
        assert [o.index for o in outs] == [0, 1, 2], "results must come back in submission order"
        assert outs[0].result.status == "ok" and "alpha" in outs[0].result.report
        assert outs[1].result.status == "failed" and "child exploded" in outs[1].result.report
        assert outs[2].result.status == "ok" and outs[2].kind == "reviewer"

        cancel = threading.Event()
        cancel.set()
        outs2 = run_fanout([FanoutTask(task="late")], tools=tools, llm=_ScriptedLLM(),
                           retriever=NullRetriever(), memory=NullMemory(), cancel=cancel)
        assert outs2[0].result.status == "cancelled", outs2[0].result
    finally:
        fo.run_scoped_agent = orig_fo
        sa.run_scoped_agent = orig


@check
def unknown_kind_and_empty_task_are_quiet_steers():
    """A hallucinated kind / empty task is a request-shape correction (↷ STEERED), matching the
    old contract — nothing failed; the surface redirects."""
    host = _host(_workspace())
    bad = host.run("spawn_agent", {"agent": "nope", "task": "x"})
    assert bad.status == ToolStatus.STEERED and "unknown agent kind" in str(bad)
    empty = host.run("spawn_agent", {"agent": "explorer", "task": "   "})
    assert empty.status == ToolStatus.STEERED and "non-empty 'task'" in str(empty)


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
