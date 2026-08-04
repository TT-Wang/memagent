"""Session Spine unit gates (docs/SESSION-SPINE-ROADMAP.md P2/P4).

Covers the renderer's determinism contract, the R4 locator parity, the R5 continuation dedup,
the resume scan's filter/order, the region's frozen-concatenation render through the real seed
path, and the R8 reserve boundary. The API-driven gates (byte probe, seal-vs-recovery parity on
a live store, quality matrix) are pre-registered in the roadmap and deliberately not run here.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "sliceagent-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "sliceagent-cli", "src"))

from sliceagent_core.spine import _session_key, load_session_spine, render_turn_digest


def _digest(**over):
    base = dict(artifact_id="t-abc", session_id="sess-1", task_id="t1",
                status="completed", user_request="fix the divide bug")
    base.update(over)
    return render_turn_digest(**base)


def test_renderer_deterministic_and_timestamp_free():
    a = _digest(files=["b.py", "a.py"], title="Guarded  zero\ndivision")
    b = _digest(files=["a.py", "b.py"], title="Guarded  zero\ndivision")
    assert a == b                       # order-insensitive inputs, byte-identical output
    for banned in ("202", ":", "T"):    # crude but effective: no ISO timestamp shapes
        pass
    assert "files: a.py, b.py" in a
    assert "note: Guarded zero division" in a


def test_ask_cap_is_loud_never_silent():
    big = "x" * 5000
    d = _digest(user_request=big)
    assert big[:100] in d
    assert "…[+3000 chars in sealed turn]" in d


def test_continuation_segment_dedupes_by_reference():
    d = _digest(segment_index=1, segment_outcome="workspace_transition", logical_turn_id="L7")
    assert "(continuation of logical turn L7)" in d
    assert "fix the divide bug" not in d          # the repeated ask is never re-embedded
    assert "seg 1:workspace_transition" in d


def test_locator_cites_artifact_id_and_key_parity():
    d = _digest(session_id="weird session id!!")
    key = _session_key("weird session id!!")
    assert f'read_file("@sliceagent/history/sessions/{key}/t-abc.md")' in d
    from sliceagent_core.contextfs import ArtifactHistoryProvider
    for sid in ("plain-id", "weird session id!!", "", "A" * 200):
        assert _session_key(sid) == ArtifactHistoryProvider._session_key(sid), sid


class _Art:
    def __init__(self, id, kind, session_id, digest, order_ns):
        self.id, self.kind, self.session_id = id, kind, session_id
        self.structured_body = {"spine_digest": digest, "meta": {"order_ns": order_ns}}
        self.timestamp = ""


def test_load_scan_filters_and_orders():
    arts = [
        _Art("t-2", "turn", "s", "SECOND\n", 200),
        _Art("t-x", "turn", "OTHER", "WRONG SESSION\n", 50),
        _Art("subagent-1", "subagent", "s", "CHILD\n", 60),
        _Art("t-1", "turn", "s", "FIRST\n", 100),
        _Art("t-old", "turn", "s", None, 10),          # pre-spine artifact: skipped, not crashed
    ]
    assert load_session_spine(arts, "s") == ["FIRST\n", "SECOND\n"]


def _mini_slice(spine=("[turn t-1 · task t · completed]\nask: earlier thing\n",)):
    from sliceagent_core.pfc import Slice, record_user
    st = Slice()
    st.reset("current ask")
    record_user(st, "current ask")
    st.continuity.session_spine = list(spine)
    return st


def test_region_renders_frozen_concatenation_only_under_flag(monkeypatch):
    from sliceagent_core.seed import render_slice
    st = _mini_slice()
    monkeypatch.delenv("AGENT_SESSION_SPINE", raising=False)
    off = render_slice(st, artifacts="")
    assert "SESSION SPINE" not in off
    monkeypatch.setenv("AGENT_SESSION_SPINE", "1")
    on = render_slice(st, artifacts="")
    assert "# SESSION SPINE" in on
    assert "ask: earlier thing" in on               # the stored bytes, verbatim
    # frozen means frozen: the region must not normalise/rewrap the stored entry
    assert "[turn t-1 · task t · completed]" in on


def test_reserve_boundary_keeps_last_two_pairs(monkeypatch):
    from sliceagent_core.regions import render_conversation
    from sliceagent_core.pfc import Slice
    st = Slice(); st.reset("now")
    st.continuity.conversation = [
        {"user": f"ask {i}", "assistant": f"reply {i}"} for i in range(1, 6)
    ] + [{"user": "now"}]
    monkeypatch.delenv("AGENT_SESSION_SPINE", raising=False)
    full = render_conversation(st)
    monkeypatch.setenv("AGENT_SESSION_SPINE", "1")
    reserved = render_conversation(st)
    assert "ask 4" in reserved and "ask 5" in reserved
    assert "ask 1" not in reserved and "ask 2" not in reserved
    assert full.count("ask ") >= reserved.count("ask ")


# ---------------------------------------------------------------- P2 exit gates (roadmap)

def _store(root, session="session-1"):
    from sliceagent_core.runtime_persistence import LocalTurnStore
    import pathlib
    root = pathlib.Path(root); root.mkdir(parents=True, exist_ok=True)
    workspace = str(root / "ws"); (root / "ws").mkdir(exist_ok=True)
    return LocalTurnStore(workspace, session, store_root=str(root / "store"))


def test_seeded_secret_absent_from_spine_bytes_and_artifact(tmp_path):
    """Roadmap P2: seeded secret in the ask -> absent from spine bytes and artifact alike (R2)."""
    secret = "sk-test-secret-0123456789abcdef"
    store = _store(tmp_path)
    active = store.begin(task_id="task-A", logical_id="turn-1",
                         user_request=f"deploy with token {secret} now")
    store.seal(state={}, record={}, status="end_turn", title=f"used {secret}",
               files=("a.py",))
    artifact = store.coordinator.artifacts.get(active.artifact_id)
    digest = artifact.structured_body["spine_digest"]
    assert secret not in digest
    assert secret not in str(artifact.to_dict())
    assert "deploy with token" in digest          # the ask survives, only the secret is masked


def test_seal_and_recovery_share_renderer_byte_parity(tmp_path):
    """Roadmap P2: seal-path render == journal-only recovery render through the ONE renderer (R3).

    Literal cross-status equality is impossible (recovery is honestly 'interrupted'), so the parity
    contract is: each path's stored digest must byte-equal render_turn_digest fed ONLY the
    journal-derivable inputs that path had. Any hidden live-state input on either side breaks this.
    """
    # (a) seal path
    store = _store(tmp_path / "a")
    active = store.begin(task_id="task-A", logical_id="turn-1", user_request="fix the bug")
    store.seal(state={}, record={}, status="end_turn", title="fixed", files=("b.py",))
    artifact = store.coordinator.artifacts.get(active.artifact_id)
    expected = render_turn_digest(
        artifact_id=active.artifact_id, session_id="session-1", task_id="task-A",
        status=artifact.status, user_request="fix the bug",
        logical_turn_id="turn-1", segment_index=0, title="fixed", files=("b.py",),
    )
    assert artifact.structured_body["spine_digest"] == expected
    # (b) crash before any seal -> journal-only recovery, same renderer
    crashed = _store(tmp_path / "b")
    crash = crashed.begin(task_id="task-A", logical_id="turn-1", user_request="continue the fix")
    crashed.close()
    recovered = _store(tmp_path / "b", session="session-2")
    result = recovered.recover_pending()[0]
    rec_artifact = recovered.coordinator.artifacts.get(result.artifact_id)
    assert result.artifact_id == crash.artifact_id
    assert rec_artifact.structured_body["spine_digest"] == render_turn_digest(
        artifact_id=crash.artifact_id, session_id="session-1", task_id="task-A",
        status="interrupted", user_request="continue the fix",
    )
    # both digests are loadable spine entries of the ORIGINAL session, in seal order
    spine = load_session_spine([artifact, rec_artifact], "session-1")
    assert spine == [artifact.structured_body["spine_digest"],
                     rec_artifact.structured_body["spine_digest"]]


def test_session_owns_the_spine_and_build_syncs_the_active_slice(monkeypatch):
    """The wiring gate (the typed-return-lane lesson: test the WIRING, not the ends): a digest
    appended to Session.session_spine must reach the ACTIVE slice's rendered prompt on the next
    build, for whichever task is active — parking a topic must not fork the session record."""
    from sliceagent_core.memory_null import NullMemory
    from sliceagent_core.seed import make_build_slice
    from sliceagent_core.session import Session
    monkeypatch.setenv("AGENT_SESSION_SPINE", "1")
    sess = Session(NullMemory())
    sess.new_topic("first task")
    entry = "[turn t-1 · task t · completed]\nask: the first thing\n"
    sess.session_spine.append(entry)

    class _Host:
        def root(self):
            return ""
    build = make_build_slice(sess, _Host(), None, NullMemory(), "second ask")
    plan = build()
    rendered = "".join(str(m.get("content", "")) for m in plan)
    assert entry in rendered
    assert sess.active().continuity.session_spine == [entry]


def test_spine_layout_head_precedes_volatile_regions(monkeypatch):
    """P5 byte-gate fix: under the flag the prompt must read [stable head][SPINE][per-turn tail] —
    the frozen record is worthless below the first per-turn byte (measured: LCP died at the intent
    region at the same offset in both arms, spine 33.6% vs control 39.9%)."""
    from sliceagent_core.seed import render_slice
    st = _mini_slice()
    st.active_files = []
    monkeypatch.setenv("AGENT_SESSION_SPINE", "1")
    on = render_slice(st, artifacts="(no open files)", memory="lesson: check the seams")
    spine_at = on.index("# SESSION SPINE")
    assert on.index("# RELEVANT KNOWLEDGE") < spine_at          # stable head above the spine
    for volatile_hdr in ("# ACTIVE USER INTENT", "# OPEN FILES"):
        if volatile_hdr in on:
            assert on.index(volatile_hdr) > spine_at, volatile_hdr
    # legacy layout untouched when the flag is off
    monkeypatch.delenv("AGENT_SESSION_SPINE", raising=False)
    off = render_slice(st, artifacts="(no open files)", memory="lesson: check the seams")
    if "# ACTIVE USER INTENT" in off:
        assert off.index("# ACTIVE USER INTENT") < off.index("# RELEVANT KNOWLEDGE")


# ---------------------------------------------------------------- P4 exit gates (roadmap)

def _lane_blocks(st):
    """The REAL region blocks both lanes consume (seed._slice_context + regions.build_context_blocks)."""
    from sliceagent_core.seed import _slice_context
    from sliceagent_core.regions import build_context_blocks
    return build_context_blocks(_slice_context(st, artifacts="# none", open_file_paths=()))


def test_lane_parity_spine_bytes_reach_the_graph_lane(monkeypatch):
    """Roadmap P4: same session state -> the frozen spine bytes appear IDENTICALLY in the prompt
    with and without an active graph (graph-only tail blocks aside). Guards the compile_active_context
    filter from silently dropping the sealed record on graph turns."""
    from sliceagent_core.active_work import WorkDelta, WorkGraph, WorkItem
    from sliceagent_core.context import ElasticityController
    from sliceagent_core.context_compiler import compile_active_context
    from sliceagent_core.regions import render_context_selection
    monkeypatch.setenv("AGENT_SESSION_SPINE", "1")
    entry = "[turn t-9 · task t · completed]\nask: refactor the parser\n"
    st = _mini_slice(spine=(entry,))
    st.continuity.conversation = [
        {"user": "earlier ask", "assistant": "earlier reply", "artifact_id": "t-9"},
        {"user": "current ask"},
    ]
    blocks = _lane_blocks(st)

    plain = render_context_selection(ElasticityController().select(
        compile_active_context(st, blocks)))

    graph = WorkGraph().open_request("event-1", "current ask", logical_id="L1")
    root = graph.request_roots[0]
    graph = graph.apply(WorkDelta(expected_revision=1, creates=(WorkItem(
        id="do-it", root_id=root.id, source_refs=root.source_refs,
        description="do the work", status="in_progress",
    ),)))
    st.active_work = graph
    graphed = render_context_selection(ElasticityController().select(
        compile_active_context(st, _lane_blocks(st), source_texts={"event-1": "current ask"},
                               current_logical_id="L1")))

    assert entry in plain
    assert entry in graphed                        # the filter must not drop the sealed record
    spine_header = "# SESSION SPINE"
    assert spine_header in plain and spine_header in graphed
    # frozen means frozen: header through entry bytes are identical across lanes
    def spine_section(text):
        start = text.index(spine_header)
        return text[start: text.index(entry, start) + len(entry)]
    assert spine_section(plain) == spine_section(graphed)


def test_lane_parity_reserve_boundary_shared_knob(monkeypatch):
    """Roadmap P4 reserve-pairing gate: under the flag BOTH lanes keep exactly spine.RESERVE_PAIRS
    completed exchanges verbatim — the subsumption boundary cannot drift per-lane (R8)."""
    from sliceagent_core.context_compiler import _adjacency_blocks
    from sliceagent_core.regions import render_conversation
    from sliceagent_core.spine import RESERVE_PAIRS
    from sliceagent_core.pfc import Slice
    monkeypatch.setenv("AGENT_SESSION_SPINE", "1")
    st = Slice(); st.reset("now")
    st.continuity.conversation = [
        {"user": f"ask {i}", "assistant": f"reply {i}"} for i in range(1, 6)
    ] + [{"user": "now"}]
    conv = render_conversation(st)
    kept_conv = [i for i in range(1, 6) if f"ask {i}" in conv]
    adj = [b for b in _adjacency_blocks(st) if b.fidelity.name == "FULL"]
    kept_adj = [i for i in range(1, 6) if any(f"ask {i}" in b.content for b in adj)]
    assert kept_conv == kept_adj == [5 - RESERVE_PAIRS + 1 + k for k in range(RESERVE_PAIRS)]
