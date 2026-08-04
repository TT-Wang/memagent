"""Session Tape mechanical gates (docs/SESSION-TAPE-DESIGN.md P-T1/P-T2).

Offline only: renderer determinism, seal-update semantics (base/patch/rebase/honesty net),
composition-hash contract, region rendering + layout under the flag, and control-path
identity with the flag off. The API gates (byte/ability/peak) are pre-registered in the
design doc and run through evals/spine_probe.py.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "sliceagent-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent_core.tape import (_h, render_tape_base, render_tape_patch,  # noqa: E402
                                  tape_seal_update)


def _ws(tmp_path, body="def f():\n    return 1\n"):
    from sliceagent.tools import LocalToolHost
    from sliceagent_core.pfc import Slice
    (tmp_path / "a.py").write_text(body, encoding="utf-8")
    tools = LocalToolHost(root=str(tmp_path))
    s = Slice(); s.reset("work")
    s.active_files = ["a.py"]
    return s, tools


def test_renderers_deterministic_and_hash_carrying():
    a = render_tape_base("a.py", "x = 1\ny = 2\n")
    assert a == render_tape_base("a.py", "x = 1\ny = 2\n")
    assert "[base a.py @sha256:" in a and "2 lines]" in a
    p = render_tape_patch("a.py", "x = 1", "x = 9", "abcdef123456")
    assert "-> @sha256:abcdef123456" in p and "<<<OLD\nx = 1\nOLD===NEW\nx = 9\nNEW>>>" in p


def test_seal_update_read_makes_base_edit_makes_patch(tmp_path):
    s, tools = _ws(tmp_path)
    rows = [("read_file", {"path": "a.py"})]
    info = tape_seal_update(s, tools, rows, session_id="s", artifact_id="t-1",
                            task_id="k", status="completed", user_request="look")
    tape = s.continuity.session_tape
    assert info["drift"] == 0 and len(tape) == 2          # digest + base
    assert tape[0].startswith("[turn t-1")
    assert "[base a.py @sha256:" in tape[1] and "def f():" in tape[1]
    # turn 2: the host applied a str_replace; disk reflects it; patch composes cleanly
    (tmp_path / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    rows = [("str_replace", {"path": "a.py", "old_string": "return 1", "new_string": "return 2"})]
    info = tape_seal_update(s, tools, rows, session_id="s", artifact_id="t-2",
                            task_id="k", status="completed", user_request="bump")
    assert info["drift"] == 0 and info["rebased"] == []
    assert "[patch a.py -> @sha256:" in tape[-1]
    # the composed hash equals the on-disk hash — the model's string-compare contract holds
    disk = (tmp_path / "a.py").read_text(encoding="utf-8")
    assert s.continuity.tape_files["a.py"]["hash"] == _h(disk)
    # frozen means frozen: earlier entries byte-identical after the second seal
    assert tape[1] == render_tape_base("a.py", "def f():\n    return 1\n")


def test_honesty_net_rebases_on_shell_write_and_drift(tmp_path):
    s, tools = _ws(tmp_path)
    tape_seal_update(s, tools, [("read_file", {"path": "a.py"})], session_id="s",
                     artifact_id="t-1", task_id="k", status="completed", user_request="look")
    # out-of-band change (shell write the recorder never saw)
    (tmp_path / "a.py").write_text("SHELL WROTE THIS\n", encoding="utf-8")
    info = tape_seal_update(s, tools, [], session_id="s", artifact_id="t-2",
                            task_id="k", status="completed", user_request="next")
    tape = s.continuity.session_tape
    assert info["drift"] == 1 and info["rebased"] == ["a.py"]
    assert any(e.startswith("[external a.py") and "changed outside" in e for e in tape)
    assert "SHELL WROTE THIS" in tape[-1]                  # fresh base = current truth
    assert s.continuity.tape_files["a.py"]["hash"] == _h("SHELL WROTE THIS\n")


def test_oversized_edit_rebases_instead_of_truncated_patch(tmp_path):
    s, tools = _ws(tmp_path)
    tape_seal_update(s, tools, [("read_file", {"path": "a.py"})], session_id="s",
                     artifact_id="t-1", task_id="k", status="completed", user_request="look")
    big = "x" * 2000
    (tmp_path / "a.py").write_text(big, encoding="utf-8")
    tape_seal_update(s, tools, [("str_replace", {"path": "a.py", "old_string": "def f():",
                                                 "new_string": big})],
                     session_id="s", artifact_id="t-2", task_id="k",
                     status="completed", user_request="big edit")
    tape = s.continuity.session_tape
    assert not any("<<<OLD" in e and big[:50] in e for e in tape)   # no truncated mega-patch
    assert tape[-1].startswith("[base a.py")                        # re-based instead


def test_region_layout_and_flags(tmp_path, monkeypatch):
    from sliceagent_core.memory_null import NullMemory
    from sliceagent_core.seed import make_build_slice
    s, tools = _ws(tmp_path)
    tape_seal_update(s, tools, [("read_file", {"path": "a.py"})], session_id="s",
                     artifact_id="t-1", task_id="k", status="completed", user_request="look")
    monkeypatch.setenv("AGENT_SESSION_TAPE", "1")
    monkeypatch.delenv("AGENT_SESSION_SPINE", raising=False)
    plan = make_build_slice(s, tools, None, NullMemory(), "edit a.py")()
    system, user = plan[0]["content"], plan[1]["content"]
    assert "WORKSPACE FILES VIA THE SESSION TAPE" in system
    assert "# SESSION TAPE" in user and "[base a.py @sha256:" in user
    assert "# SESSION SPINE" not in user                    # tape absorbs the spine
    # index (locators) present, full bodies absent from OPEN FILES
    assert "# OPEN FILES (index" in user
    tape_at = user.index("# SESSION TAPE")
    assert user.index("# OPEN FILES") > tape_at             # tape above the volatile tail
    if "# ACTIVE USER INTENT" in user:
        assert user.index("# ACTIVE USER INTENT") > tape_at
    # flag off -> control path: no tape region, bodies render
    monkeypatch.delenv("AGENT_SESSION_TAPE", raising=False)
    off = make_build_slice(s, tools, None, NullMemory(), "edit a.py")()
    assert "# SESSION TAPE" not in off[1]["content"]
    assert "def f():" in off[1]["content"]


def test_seeded_secret_absent_from_tape(tmp_path):
    secret = "sk-test-secret-0123456789abcdef"
    s, tools = _ws(tmp_path, body=f"token = '{secret}'\n")
    tape_seal_update(s, tools, [("read_file", {"path": "a.py"})], session_id="s",
                     artifact_id="t-1", task_id="k", status="completed", user_request="look")
    assert secret not in "".join(s.continuity.session_tape)
