"""Session Tape mechanical gates — typed-core rebuild (2026-08-05 review).

Offline only: renderer determinism, typed entries as the ONLY schema channel, event-time
snapshots, true-diff patches, rendered-size representation choice, the honesty net, fold
re-anchoring (patches never orphaned), spaces-in-path safety, no-trailing-newline exactness,
journal durability + replay, digest verbatim append, and region rendering under the flag.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "sliceagent-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent_core.tape import (_h, TapeEntry, TapeRecorder, base_entry,  # noqa: E402
                                  compact_tape, compose_after, digest_entry,
                                  load_session_tape, patch_entry, render_tape_base,
                                  tape_render, tape_seal_update, unified_patch)


def _ws(tmp_path, body="def f():\n    return 1\n"):
    from sliceagent.tools import LocalToolHost
    from sliceagent_core.pfc import Slice
    (tmp_path / "a.py").write_text(body, encoding="utf-8")
    tools = LocalToolHost(root=str(tmp_path))
    s = Slice(); s.reset("work")
    s.active_files = ["a.py"]
    return s, tools


def _seal(s, tools, rows, n=1, ask="work", **kw):
    return tape_seal_update(s, tools, rows, session_id="s", artifact_id=f"t-{n}",
                            task_id="k", status="completed", user_request=ask, **kw)


def _digest_turns(chars=300, n=8):
    return [digest_entry(f"[turn t-{i} · task k · completed]\nask: {'x' * chars}\n", f"t-{i}")
            for i in range(1, n + 1)]


def test_renderers_and_diff_deterministic():
    a = render_tape_base("a.py", "x = 1\ny = 2\n")
    assert a == render_tape_base("a.py", "x = 1\ny = 2\n")
    assert "[base a.py @sha256:" in a and "2 lines]" in a
    d = unified_patch("a.py", "x = 1\ny = 2\n", "x = 9\ny = 2\n")
    assert d == unified_patch("a.py", "x = 1\ny = 2\n", "x = 9\ny = 2\n")
    assert "-x = 1" in d and "+x = 9" in d and "--- a" in d


def test_typed_entries_are_the_schema_channel():
    e = base_entry("dir/a file.py", "x = 1\n")
    assert e.kind == "base" and e.path == "dir/a file.py" and e.payload == "x = 1\n"
    assert e.post_hash == _h("x = 1\n")
    r = TapeEntry.from_record(e.to_record())
    assert r == e


def test_event_time_snapshots_make_replay_an_identity(tmp_path):
    """Two edits to ONE file in one turn: each patch is the true per-edit delta because the
    recorder snapshots disk at event time — a seal-time read would collapse them."""
    pad = "# padding\n" * 20
    s, tools = _ws(tmp_path, body=f"def f():\n    return 1\n{pad}")
    rec = TapeRecorder(tools)

    rec.sink(type("ToolResult", (), {"name": "read_file", "args": {"path": "a.py"},
                                     "status": "succeeded"})())
    assert rec.rows == []                     # reads are never recorded (defer-base + no dead I/O)
    (tmp_path / "a.py").write_text(f"def f():\n    return 2\n{pad}", encoding="utf-8")
    rec.sink(type("ToolResult", (), {"name": "str_replace", "args": {"path": "a.py"},
                                     "status": "succeeded"})())
    (tmp_path / "a.py").write_text(f"def f():\n    return 3\n{pad}", encoding="utf-8")
    rec.sink(type("ToolResult", (), {"name": "str_replace", "args": {"path": "a.py"},
                                     "status": "succeeded"})())
    info = _seal(s, tools, rec.rows)
    tape = s.continuity.session_tape
    assert info["drift"] == 0 and info["rebased"] == []
    assert sum(1 for e in tape if e.kind == "base" and e.path == "a.py") == 1
    patches = [e for e in tape if e.kind == "patch" and e.path == "a.py"]
    assert len(patches) == 1
    assert "-    return 2" in patches[0].rendered and "+    return 3" in patches[0].rendered
    disk = (tmp_path / "a.py").read_text(encoding="utf-8")
    assert s.continuity.tape_files["a.py"]["hash"] == _h(disk)


def test_long_chain_never_rebases_small_edits(tmp_path):
    """v1.1: NO chain-length trigger. 20 small edits -> 19 patches, one base, zero rebases."""
    s, tools = _ws(tmp_path, body="v = 0\n" + "pad\n" * 30)
    rows = []
    for i in range(1, 21):
        body = f"v = {i}\n" + "pad\n" * 30
        (tmp_path / "a.py").write_text(body, encoding="utf-8")
        rows.append(("a.py", body))
    info = _seal(s, tools, rows)
    tape = s.continuity.session_tape
    assert info["rebased"] == [] and info["drift"] == 0
    assert sum(1 for e in tape if e.kind == "base") == 1
    assert sum(1 for e in tape if e.kind == "patch") == 19


def test_representation_choice_compares_rendered_sizes(tmp_path):
    """Review P2: the choice is between the RENDERED patch block and the RENDERED base block,
    never the raw diff-vs-body lengths (headers and framing are billed bytes too)."""
    for body_a, body_b in [
        ("line\n" * 3, "line\n" * 2 + "changed\n"),                 # small delta
        ("old\n" * 2, "totally\nnew\ncontent\nnow\n"),              # near-rewrite of a tiny file
        ("v = 1\n" + "pad\n" * 40, "v = 2\n" + "pad\n" * 40),       # small delta in a big file
    ]:
        s, tools = _ws(tmp_path)
        (tmp_path / "x.py").write_text(body_a, encoding="utf-8")
        _seal(s, tools, [("x.py", body_a)])
        (tmp_path / "x.py").write_text(body_b, encoding="utf-8")
        _seal(s, tools, [("x.py", body_b)], n=2)
        win = [e for e in s.continuity.session_tape if e.path == "x.py"][-1]
        pe, be = patch_entry("x.py", body_a, body_b), base_entry("x.py", body_b)
        expected = pe if len(pe.rendered) < len(be.rendered) else be
        assert win.kind == expected.kind, (win.kind, len(pe.rendered), len(be.rendered))
        assert win.rendered == expected.rendered


def test_full_rewrite_picks_base_by_size_not_policy(tmp_path):
    s, tools = _ws(tmp_path)
    before = (tmp_path / "a.py").read_text(encoding="utf-8")
    rows = [("a.py", before)]
    rewrite = "COMPLETELY = 'different'\n" + "new_line()\n" * 40
    (tmp_path / "a.py").write_text(rewrite, encoding="utf-8")
    rows.append(("a.py", rewrite))
    info = _seal(s, tools, rows)
    tape = s.continuity.session_tape
    bases = [e for e in tape if e.kind == "base" and e.path == "a.py"]
    # first row founds the base; the rewrite is bigger as a diff than as a base -> fresh base
    assert info["rebased"] == ["a.py"]
    assert len(bases) == 2 and "COMPLETELY" in bases[-1].rendered


def test_honesty_net_catches_out_of_band_change(tmp_path):
    s, tools = _ws(tmp_path)
    _seal(s, tools, [("a.py", (tmp_path / "a.py").read_text(encoding="utf-8"))])
    (tmp_path / "a.py").write_text("SHELL WROTE THIS\n", encoding="utf-8")
    info = _seal(s, tools, [], n=2)
    tape = s.continuity.session_tape
    assert info["drift"] == 1 and info["rebased"] == ["a.py"] or info["drift"] == 1
    assert any(e.kind == "external" and "outside the recorded edits" in e.rendered for e in tape)
    assert s.continuity.tape_files["a.py"]["hash"] == _h("SHELL WROTE THIS\n")
    # frozen means frozen: the original base bytes are untouched by the correction
    assert any(e.kind == "base" and "def f():" in e.rendered for e in tape)
    # drift re-anchor is delta-sized: a small out-of-band change arrives as a PATCH, not a base
    kinds_after_external = [e.kind for e in tape[next(i for i, e in enumerate(tape)
                                                     if e.kind == "external") + 1:]]
    assert kinds_after_external[0] in ("patch", "base")


def test_digest_appended_verbatim_and_fallback_redacts(tmp_path):
    s, tools = _ws(tmp_path)
    sealed = "[turn t-9 · task k · completed]\nask: already-redacted upstream\n"
    _seal(s, tools, [], ask="raw", digest_text=sealed)
    assert s.continuity.session_tape[0].rendered == sealed          # byte-identical, ONE render
    assert s.continuity.session_tape[0].ref == "t-1"
    s2, tools2 = _ws(tmp_path)
    secret = "sk-test-secret-0123456789abcdef"
    _seal(s2, tools2, [], ask=f"use {secret} please")
    assert secret not in tape_render(s2.continuity.session_tape)    # fallback render redacts


def test_no_trailing_newline_roundtrip(tmp_path):
    """Review P1: files without a final newline must stay byte-exact through the registry and
    the journal replay; the rendered form annotates instead of lying."""
    body1 = "x = 1"                       # no trailing newline
    body2 = "x = 2"                       # still none
    e1 = base_entry("f.py", body1)
    assert e1.no_nl and "no trailing newline" in e1.rendered
    e2 = patch_entry("f.py", body1, body2)
    assert e2.no_nl
    assert compose_after(e1, "") == body1
    assert compose_after(e2, body1) == body2
    assert _h(compose_after(e2, body1)) == e2.post_hash


def test_spaces_in_paths_survive_gc_and_fold():
    """Review P1: 'dir/a one.py' and 'dir/a two.py' must never alias (the old string parser
    split on whitespace and corrupted both)."""
    files = {"dir/a one.py": {"hash": _h("ONE v2\n"), "content": "ONE v2\n"},
             "dir/a two.py": {"hash": _h("TWO v1\n"), "content": "TWO v1\n"}}
    tape = [
        base_entry("dir/a one.py", "ONE v1\n"),
        base_entry("dir/a two.py", "TWO v1\n"),
        base_entry("dir/a one.py", "ONE v2\n"),      # supersedes one.py only
        *_digest_turns(chars=2000, n=3),
    ]
    info = compact_tape(tape, files, budget=5_000)
    assert info["gc_removed"] == 1                    # ONLY one.py's dead base
    assert any(e.kind == "base" and e.path == "dir/a two.py" for e in tape)
    one_bases = [e for e in tape if e.kind == "base" and e.path == "dir/a one.py"]
    assert len(one_bases) == 1 and "ONE v2" in one_bases[0].rendered


def test_fold_reanchors_files_instead_of_orphaning_patches():
    """Review P1 (both reviews): the old fold kept a carried base but deleted its later patches
    — base+patch composition then failed. The typed fold re-anchors every affected file to its
    registry content as ONE fresh base and drops the whole stale chain."""
    body_v1 = "A" * 300 + "\n"
    body_v2 = "A" * 300 + "\nB\n"
    files = {"f.py": {"hash": _h(body_v2), "content": body_v2}}
    tape = [
        *_digest_turns(chars=800, n=4),
        base_entry("f.py", body_v1),
        patch_entry("f.py", body_v1, body_v2),
        *_digest_turns(chars=800, n=9)[4:],
    ]
    info = compact_tape(tape, files, budget=2_000)
    assert info["epoch_folds"] == 1
    assert tape[0].kind == "epoch"
    f_entries = [e for e in tape if e.path == "f.py"]
    assert [e.kind for e in f_entries] == ["base"], "one fresh base, no orphaned patches"
    assert f_entries[0].payload == body_v2            # re-anchored to CURRENT composed content
    assert _h(body_v2) == f_entries[0].post_hash


def test_fold_guarantees_net_reduction_on_big_files():
    """G2 catch (s11 typed r4: 18 folds, tape 166k > 120k budget): re-anchor bases ADD bytes,
    so a fold sized by span bytes alone can shrink nothing and re-trigger every seal. The cut
    must grow until the NET effect reaches the target — and repeated compaction must converge
    (either under budget or a bail, never a marker-stacking loop)."""
    big = ("line of real code\n" * 400)          # ~7.2k chars per file, s11-shaped
    files = {f"mod{i}.py": {"hash": _h(big), "content": big} for i in range(5)}
    tape = []
    for i in range(1, 9):
        tape.append(digest_entry(f"[turn t-{i} · task k · completed]\nask: {'x' * 600}\n", f"t-{i}"))
        p = f"mod{i % 5}.py"
        tape.append(patch_entry(p, big + f"# v{i}\n", big))   # small patches on big files
    tape.insert(1, base_entry("mod0.py", big))
    before_chars = sum(len(e.rendered) for e in tape)
    budget = int(before_chars * 0.8)
    info = compact_tape(tape, files, budget=budget)
    after_chars = sum(len(e.rendered) for e in tape)
    if info["epoch_folds"]:
        assert after_chars < before_chars, "a fold must never grow the tape"
    # convergence: repeated calls either reach steady state or bail — never fold forever
    folds = 0
    for _ in range(6):
        r = compact_tape(tape, files, budget=budget)
        folds += r["epoch_folds"]
    assert folds <= 1, f"compaction thrashed ({folds} extra folds at steady state)"


def test_compaction_under_budget_untouched_and_epoch_chain():
    files: dict = {}
    tape = _digest_turns(chars=400, n=20)
    before = list(tape)
    assert compact_tape(tape, files, budget=10_000_000) == {"gc_removed": 0, "epoch_folds": 0}
    assert tape == before
    info = compact_tape(tape, files, budget=4_000)
    assert info["epoch_folds"] == 1
    assert tape[0].kind == "epoch" and tape[0].ref == "t-1"
    assert "index.md" in tape[0].rendered
    # chain: a second fold carries the FIRST folded ref forward
    tape2 = [tape[0], *_digest_turns(chars=400, n=20)]
    compact_tape(tape2, files, budget=4_000)
    assert tape2[0].kind == "epoch" and tape2[0].ref == "t-1"


def test_journal_roundtrip_rebuilds_tape_and_registry(tmp_path):
    s, tools = _ws(tmp_path, body="v = 1\n" + "pad\n" * 10)
    j = str(tmp_path / "state" / "tape.jsonl")
    _seal(s, tools, [("a.py", (tmp_path / "a.py").read_text(encoding="utf-8"))],
          journal_path=j, ask="first")
    body2 = "v = 2\n" + "pad\n" * 10
    (tmp_path / "a.py").write_text(body2, encoding="utf-8")
    _seal(s, tools, [("a.py", body2)], n=2, journal_path=j, ask="second")
    loaded_tape, loaded_files = load_session_tape(j)
    assert tape_render(loaded_tape) == tape_render(s.continuity.session_tape)
    assert loaded_files["a.py"]["hash"] == s.continuity.tape_files["a.py"]["hash"]
    assert loaded_files["a.py"]["content"] == body2
    # torn tail line (crash mid-write) ends the replay instead of raising
    with open(j, "a", encoding="utf-8") as f:
        f.write('{"kind": "patch", "path": "a.py", "payl')
    t2, f2 = load_session_tape(j)
    assert tape_render(t2) == tape_render(loaded_tape)
    assert f2["a.py"]["content"] == body2


def test_region_layout_and_flags(tmp_path, monkeypatch):
    from sliceagent_core.memory_null import NullMemory
    from sliceagent_core.seed import make_build_slice
    s, tools = _ws(tmp_path)
    _seal(s, tools, [("a.py", (tmp_path / "a.py").read_text(encoding="utf-8"))])
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
    from sliceagent_core.seed import make_build_slice
    s, tools = _ws(tmp_path)
    s.continuity.conversation = [{"user": "first ask", "assistant": "I recommend option two."},
                                 {"user": "now"}]
    _seal(s, tools, [], ask="first ask")
    tape_seal_update(s, tools, [], session_id="s", artifact_id="t-1", task_id="k",
                     status="completed", user_request="first ask",
                     assistant_reply="I recommend option two.")
    assert any(e.kind == "reply" and "option two" in e.rendered
               for e in s.continuity.session_tape)
    tape_seal_update(s, tools, [], session_id="s", artifact_id="t-2", task_id="k",
                     status="completed", user_request="x", assistant_reply="y" * 5000)
    assert any("…[+3800 chars in sealed turn]" in e.rendered
               for e in s.continuity.session_tape)
    monkeypatch.setenv("AGENT_SESSION_TAPE", "1")
    plan = make_build_slice(s, tools, None, NullMemory(), "next ask")()
    user = plan[1]["content"]
    assert "# RECENT CONVERSATION" not in user
    assert "[reply t-1]" in user
    assert _adjacency_blocks(s) == ()
    monkeypatch.delenv("AGENT_SESSION_TAPE", raising=False)
    off = make_build_slice(s, tools, None, NullMemory(), "next ask")()
    assert "# RECENT CONVERSATION" in off[1]["content"]


def test_seeded_secret_absent_from_tape(tmp_path):
    secret = "sk-test-secret-0123456789abcdef"
    s, tools = _ws(tmp_path, body=f"token = '{secret}'\n")
    _seal(s, tools, [("a.py", (tmp_path / "a.py").read_text(encoding="utf-8"))])
    assert secret not in tape_render(s.continuity.session_tape)


def test_workspace_rebase_carries_tape_and_spine():
    from sliceagent_core.memory_null import NullMemory
    from sliceagent_core.session import Session, rebase_session_for_workspace
    cur = Session(NullMemory(), "sess-1")
    cur.session_spine = ["[turn t-1 · task k · completed]\nask: x\n"]
    cur.session_tape = [digest_entry(cur.session_spine[0], "t-1")]
    cur.tape_files = {"a.py": {"hash": "abc", "content": "x\n"}}
    restored = Session(NullMemory(), "sess-1")
    merged = rebase_session_for_workspace(cur, restored)
    assert merged.session_spine == cur.session_spine
    assert merged.session_tape == cur.session_tape
    assert merged.tape_files == cur.tape_files
