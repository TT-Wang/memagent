"""Option B mechanical gates (docs/OPENFILES-SUBSUMPTION-DESIGN.md).

Offline only: locator rendering shape, turn-start snapshot semantics, control-path identity with
the flag off, and the coupled system-prompt discipline. The pre-registered API gates (ability /
byte / liveness) live in the design doc and run through evals/spine_probe.py.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "sliceagent-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _ws(tmp_path, body="def f():\n    return 1\n"):
    from sliceagent.tools import LocalToolHost
    from sliceagent_core.pfc import Slice
    (tmp_path / "a.py").write_text(body, encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 2\n", encoding="utf-8")
    tools = LocalToolHost(root=str(tmp_path))
    s = Slice(); s.reset("edit the files")
    s.active_files = ["a.py", "b.py", "missing.py"]
    s.edited_files = {"b.py"}
    return s, tools


def test_locator_lines_carry_the_full_visible_manifest_recipe(tmp_path):
    from sliceagent_core.seed import render_file_locators
    s, tools = _ws(tmp_path)
    out = render_file_locators(s, tools)
    lines = out.splitlines()
    assert lines[0].startswith("### b.py — 1 lines · sha256:")   # edited files sort first
    assert lines[0].endswith('read_file("b.py") to view · (edited this session)')
    assert '### a.py — 2 lines · sha256:' in out
    assert 'read_file("a.py") to view' in out
    assert "### missing.py (not created yet)" in out             # error state, hashless
    import re
    assert re.search(r"sha256:[0-9a-f]{12} ", out)
    assert "```" not in out and "def f()" not in out             # NO file bodies, ever


def test_flag_off_control_path_renders_bodies_unchanged(tmp_path, monkeypatch):
    from sliceagent_core.seed import build_artifacts
    monkeypatch.delenv("AGENT_OPENFILES_LOCATORS", raising=False)
    s, tools = _ws(tmp_path)
    art = build_artifacts(s, tools)
    assert "def f():" in art and "```" in art                    # bodies still render on control


def test_turn_start_snapshot_holds_within_a_turn_and_refreshes_next_turn(tmp_path, monkeypatch):
    from sliceagent_core.memory_null import NullMemory
    from sliceagent_core.seed import make_build_slice
    monkeypatch.setenv("AGENT_OPENFILES_LOCATORS", "1")
    s, tools = _ws(tmp_path)
    build = make_build_slice(s, tools, None, NullMemory(), "edit the files")
    first = build()[1]["content"]
    # disk mutates mid-turn -> the locator block must NOT move (turn-start snapshot)
    (tmp_path / "a.py").write_text("def f():\n    return 999\n\n\n# more\n", encoding="utf-8")
    again = build()[1]["content"]
    assert first == again
    # a new turn re-snapshots: hash and line count update
    s.turns += 1
    fresh = build()[1]["content"]
    assert fresh != first and "— 5 lines" in fresh


def test_discipline_prompt_and_header_ship_together_with_the_flag(tmp_path, monkeypatch):
    from sliceagent_core.memory_null import NullMemory
    from sliceagent_core.seed import make_build_slice
    s, tools = _ws(tmp_path)
    monkeypatch.setenv("AGENT_OPENFILES_LOCATORS", "1")
    plan = make_build_slice(s, tools, None, NullMemory(), "edit the files")()
    system, user = plan[0]["content"], plan[1]["content"]
    assert "WORKSPACE FILES ARE LOCATORS" in system
    assert "Before ANY edit" in system
    assert "# OPEN FILES (locators — contents NOT in context" in user
    assert "live — your ground truth" not in user
    monkeypatch.delenv("AGENT_OPENFILES_LOCATORS", raising=False)
    off = make_build_slice(s, tools, None, NullMemory(), "edit the files")()
    assert "WORKSPACE FILES ARE LOCATORS" not in off[0]["content"]
    assert "live — your ground truth" in off[1]["content"]
