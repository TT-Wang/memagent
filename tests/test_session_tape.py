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
    assert "-x = 1" in d and "+x = 9" in d and "a/a.py" in d


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


def test_seeded_secret_absent_from_tape(tmp_path):
    secret = "sk-test-secret-0123456789abcdef"
    s, tools = _ws(tmp_path, body=f"token = '{secret}'\n")
    _seal(s, tools, [("read", "a.py", (tmp_path / "a.py").read_text(encoding="utf-8"))])
    assert secret not in "".join(s.continuity.session_tape)
