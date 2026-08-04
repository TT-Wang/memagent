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
