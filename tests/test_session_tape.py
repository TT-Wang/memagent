"""Session Tape mechanical gates (docs/SESSION-TAPE-DESIGN.md P-T1/P-T2, v1.1 reactive-rebase).

Offline only: renderer determinism, event-time snapshot capture, true-diff patches (identity
replay), representation choice (diff vs base by size, no chain policy), the honesty net, region
rendering + layout under the flag, and control-path identity with the flag off.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "sliceagent-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent_core.tape import (_h, TapeRecorder, render_tape_base,  # noqa: E402
                                  tape_seal_update, unified_patch)


def _ws(tmp_path, body="def f():\n    return 1\n"):
    from sliceagent.tools import LocalToolHost
    from sliceagent_core.pfc import Slice
    (tmp_path / "a.py").write_text(body, encoding="utf-8")
    tools = LocalToolHost(root=str(tmp_path))
    s = Slice(); s.reset("work")
    s.active_files = ["a.py"]
    return s, tools


def _seal(s, tools, rows, n=1, ask="work"):
    return tape_seal_update(s, tools, rows, session_id="s", artifact_id=f"t-{n}",
                            task_id="k", status="completed", user_request=ask)


def test_renderers_and_diff_deterministic():
    a = render_tape_base("a.py", "x = 1\ny = 2\n")
    assert a == render_tape_base("a.py", "x = 1\ny = 2\n")
    assert "[base a.py @sha256:" in a and "2 lines]" in a
    d = unified_patch("a.py", "x = 1\ny = 2\n", "x = 9\ny = 2\n")
    assert d == unified_patch("a.py", "x = 1\ny = 2\n", "x = 9\ny = 2\n")
    assert "-x = 1" in d and "+x = 9" in d and "--- a" in d


def test_event_time_snapshots_make_replay_an_identity(tmp_path):
    """Two edits to ONE file in one turn: each patch is the true per-edit delta because the
    recorder snapshots disk at event time — a seal-time read would collapse them."""
    pad = "# padding\n" * 20
    s, tools = _ws(tmp_path, body=f"def f():\n    return 1\n{pad}")
    rec = TapeRecorder(tools)

    rec.sink(type("ToolResult", (), {"name": "read_file", "args": {"path": "a.py"},
                                     "status": "succeeded"})())
    (tmp_path / "a.py").write_text(f"def f():\n    return 2\n{pad}", encoding="utf-8")
    rec.sink(type("ToolResult", (), {"name": "str_replace", "args": {"path": "a.py"},
                                     "status": "succeeded"})())
    (tmp_path / "a.py").write_text(f"def f():\n    return 3\n{pad}", encoding="utf-8")
    rec.sink(type("ToolResult", (), {"name": "str_replace", "args": {"path": "a.py"},
                                     "status": "succeeded"})())
    info = _seal(s, tools, rec.rows)
    tape = s.continuity.session_tape
    assert info["drift"] == 0 and info["rebased"] == []
    patches = [e for e in tape if e.startswith("[patch a.py")]
    assert len(patches) == 2
    assert "-    return 1" in patches[0] and "+    return 2" in patches[0]
    assert "-    return 2" in patches[1] and "+    return 3" in patches[1]
    disk = (tmp_path / "a.py").read_text(encoding="utf-8")
    assert s.continuity.tape_files["a.py"]["hash"] == _h(disk)


def test_long_chain_never_rebases_small_edits(tmp_path):
    """v1.1: NO chain-length trigger. 20 small edits -> 20 patches, one base, zero rebases."""
    s, tools = _ws(tmp_path, body="v = 0\n" + "pad\n" * 30)
    rows = [("read", "a.py", (tmp_path / "a.py").read_text(encoding="utf-8"))]
    for i in range(1, 21):
        body = f"v = {i}\n" + "pad\n" * 30
        (tmp_path / "a.py").write_text(body, encoding="utf-8")
        rows.append(("edit", "a.py", body))
    info = _seal(s, tools, rows)
    tape = s.continuity.session_tape
    assert info["rebased"] == [] and info["drift"] == 0
    assert sum(1 for e in tape if e.startswith("[base a.py")) == 1
    assert sum(1 for e in tape if e.startswith("[patch a.py")) == 20


def test_full_rewrite_picks_base_by_size_not_policy(tmp_path):
    s, tools = _ws(tmp_path)
    before = (tmp_path / "a.py").read_text(encoding="utf-8")
    rows = [("read", "a.py", before)]
    rewrite = "COMPLETELY = 'different'\n" + "new_line()\n" * 40
    (tmp_path / "a.py").write_text(rewrite, encoding="utf-8")
    rows.append(("edit", "a.py", rewrite))
    info = _seal(s, tools, rows)
    tape = s.continuity.session_tape
    assert info["rebased"] == ["a.py"]                      # diff >= body -> fresh base wins
    assert sum(1 for e in tape if e.startswith("[base a.py")) == 2
    assert not any(e.startswith("[patch a.py") for e in tape)


def test_honesty_net_catches_out_of_band_change(tmp_path):
    s, tools = _ws(tmp_path)
    _seal(s, tools, [("read", "a.py", (tmp_path / "a.py").read_text(encoding="utf-8"))])
    (tmp_path / "a.py").write_text("SHELL WROTE THIS\n", encoding="utf-8")
    info = _seal(s, tools, [], n=2)
    tape = s.continuity.session_tape
    assert info["drift"] == 1 and info["rebased"] == ["a.py"]
    assert any(e.startswith("[external a.py") and "outside the recorded edits" in e for e in tape)
    assert "SHELL WROTE THIS" in tape[-1]
    assert s.continuity.tape_files["a.py"]["hash"] == _h("SHELL WROTE THIS\n")
    # frozen means frozen: the original base bytes are untouched by the correction
    assert any(e == render_tape_base("a.py", "def f():\n    return 1\n") for e in tape)


def test_region_layout_and_flags(tmp_path, monkeypatch):
    from sliceagent_core.memory_null import NullMemory
    from sliceagent_core.seed import make_build_slice
    s, tools = _ws(tmp_path)
    _seal(s, tools, [("read", "a.py", (tmp_path / "a.py").read_text(encoding="utf-8"))])
    monkeypatch.setenv("AGENT_SESSION_TAPE", "1")
    monkeypatch.delenv("AGENT_SESSION_SPINE", raising=False)
    plan = make_build_slice(s, tools, None, NullMemory(), "edit a.py")()
    system, user = plan[0]["content"], plan[1]["content"]
    assert "WORKSPACE FILES VIA THE SESSION TAPE" in system
    assert "unified diff" in system
    assert "# SESSION TAPE" in user and "[base a.py @sha256:" in user
    assert "# SESSION SPINE" not in user
    assert "# OPEN FILES (index" in user
    tape_at = user.index("# SESSION TAPE")
    assert user.index("# OPEN FILES") > tape_at
    monkeypatch.delenv("AGENT_SESSION_TAPE", raising=False)
    off = make_build_slice(s, tools, None, NullMemory(), "edit a.py")()
    assert "# SESSION TAPE" not in off[1]["content"]
    assert "def f():" in off[1]["content"]


def test_reply_entry_replaces_conversation_region(tmp_path, monkeypatch):
    """A2: the turn's outward answer freezes into the tape (billed once); the RECENT CONVERSATION
    region and the graph adjacency lane render nothing under the tape."""
    from sliceagent_core.context_compiler import _adjacency_blocks
    from sliceagent_core.memory_null import NullMemory
    from sliceagent_core.pfc import Slice
    from sliceagent_core.seed import make_build_slice
    s, tools = _ws(tmp_path)
    s.continuity.conversation = [{"user": "first ask", "assistant": "I recommend option two."},
                                 {"user": "now"}]
    tape_seal_update(s, tools, [], session_id="s", artifact_id="t-1", task_id="k",
                     status="completed", user_request="first ask",
                     assistant_reply="I recommend option two.")
    assert any(e.startswith("[reply t-1]") and "option two" in e
               for e in s.continuity.session_tape)
    big = tape_seal_update(s, tools, [], session_id="s", artifact_id="t-2", task_id="k",
                          status="completed", user_request="x", assistant_reply="y" * 5000)
    assert any("…[+3800 chars in sealed turn]" in e for e in s.continuity.session_tape)
    monkeypatch.setenv("AGENT_SESSION_TAPE", "1")
    plan = make_build_slice(s, tools, None, NullMemory(), "next ask")()
    user = plan[1]["content"]
    assert "# RECENT CONVERSATION" not in user
    assert "[reply t-1]" in user
    assert _adjacency_blocks(s) == ()
    monkeypatch.delenv("AGENT_SESSION_TAPE", raising=False)
    off = make_build_slice(s, tools, None, NullMemory(), "next ask")()
    assert "# RECENT CONVERSATION" in off[1]["content"]


def test_compaction_contract_gc_then_epoch_fold(tmp_path):
    """A3: over budget -> dead file history GC'd first, then oldest span folds to ONE epoch
    marker with a locator; under budget -> frozen bytes untouched."""
    from sliceagent_core.tape import compact_tape, render_tape_base
    tape = [f"[turn t-{i} · task k · completed]\nask: step {i}\n" for i in range(1, 4)]
    tape.insert(1, render_tape_base("a.py", "old " * 300))
    tape.insert(2, "[patch a.py -> @sha256:aaa · unified diff of the edit you made]\n-x\n+y\n")
    tape.append(render_tape_base("a.py", "new " * 300))          # supersedes the old history
    before = list(tape)
    assert compact_tape(tape, budget=10_000_000) == {"gc_removed": 0, "epoch_folds": 0}
    assert tape == before                                        # under budget: untouched
    info = compact_tape(tape, budget=2_000)
    assert info["gc_removed"] == 2                               # dead base+patch removed
    assert not any("old old" in e for e in tape)
    assert any("new new" in e for e in tape)                     # latest base survives
    tape2 = [f"[turn t-{i} · task k · completed]\nask: {'x' * 400}\n" for i in range(1, 21)]
    info2 = compact_tape(tape2, budget=4_000)
    assert info2["epoch_folds"] >= 1
    assert tape2[0].startswith("[epoch compacted: t-1..")
    assert "index.md" in tape2[0]
    assert sum(len(e) for e in tape2) <= 4_000


def test_seeded_secret_absent_from_tape(tmp_path):
    secret = "sk-test-secret-0123456789abcdef"
    s, tools = _ws(tmp_path, body=f"token = '{secret}'\n")
    _seal(s, tools, [("read", "a.py", (tmp_path / "a.py").read_text(encoding="utf-8"))])
    assert secret not in "".join(s.continuity.session_tape)


def test_fold_carries_live_bases_and_bails_when_nothing_foldable(tmp_path):
    """s11 FAIL@33 root cause: a blind fold swallowed live file bases and the composition
    contract forced re-reads. The type-aware fold carries every LATEST base across the fold
    and bails (no empty marker) when only working set remains."""
    from sliceagent_core.tape import compact_tape, render_tape_base
    base_a = render_tape_base("a.py", "A " * 400)
    tape = [f"[turn t-{i} · task k · completed]\nask: {'x' * 300}\n" for i in range(1, 9)]
    tape.insert(2, base_a)                                   # live base buried in old history
    info = compact_tape(tape, budget=2_500)
    assert info["epoch_folds"] == 1
    assert tape[0].startswith("[epoch compacted:")
    assert any(e == base_a for e in tape), "live base must survive the fold"
    # nothing-foldable bail: a tape that is ONLY live bases never stacks empty markers
    only_bases = [render_tape_base(f"f{i}.py", "B " * 500) for i in range(6)]
    before = list(only_bases)
    info2 = compact_tape(only_bases, budget=1_000)
    assert info2["epoch_folds"] == 0 and only_bases == before
