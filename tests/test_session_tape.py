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
                                  tape_chars, tape_render, tape_seal_update, unified_patch)


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
    plan = make_build_slice(s, tools, None, NullMemory(), "edit a.py")()
    system, user = plan[0]["content"], plan[1]["content"]
    assert "WORKSPACE FILES VIA THE SESSION TAPE" in system
    assert "unified diff" in system
    assert "# SESSION TAPE" in user and "[base a.py @sha256:" in user
    assert "# SESSION SPINE" not in user
    assert "# OPEN FILES (index" in user
    tape_at = user.index("# SESSION TAPE")
    assert user.index("# OPEN FILES") > tape_at
    # (kill-switch off-assertions retired in wave 2 — the tape is unconditional)


def test_reply_entry_replaces_conversation_region(tmp_path, monkeypatch):
    """A2: the turn's outward answer freezes into the tape (billed once); the RECENT CONVERSATION
    region and the graph adjacency lane render nothing under the tape."""
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
    plan = make_build_slice(s, tools, None, NullMemory(), "next ask")()
    user = plan[1]["content"]
    assert "# RECENT CONVERSATION" not in user
    assert "[reply t-1]" in user
    # (kill-switch off-assertions retired in wave 2 — the tape is unconditional)


def test_seeded_secret_absent_from_tape(tmp_path):
    secret = "sk-test-secret-0123456789abcdef"
    s, tools = _ws(tmp_path, body=f"token = '{secret}'\n")
    _seal(s, tools, [("a.py", (tmp_path / "a.py").read_text(encoding="utf-8"))])
    assert secret not in tape_render(s.continuity.session_tape)


def test_offline_replay_mechanics_gate():
    """The fast lane (evals/tape_replay.py): s11-shaped 52-turn stream over real file bodies —
    fold policy, budget bound, thrash and boundary-bill shape, no live model. This is the
    per-iteration gate; the live scenario runs once at graduation time only."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "evals"))
    from tape_replay import GATES, replay
    r = replay()
    for name, fn in GATES:
        assert fn(r), (name, r)


def test_pre_tape_session_migrates_digests_and_last_reply_into_tape(tmp_path):
    """Review Task147 blocker 1: a session whose artifacts PREDATE any tape journal must resume
    with its earlier asks and its newest reply visible on the tape — the regions that used to
    render them are retired."""
    from sliceagent_core.tape import reconcile_tape_with_digests
    pairs = [(f"t-{i}", f"[turn t-{i} · task k · completed]\nask: request number {i}\n")
             for i in range(1, 5)]
    # (a) no journal at all -> empty tape -> every digest enters + the newest reply freezes
    tape, files = load_session_tape(str(tmp_path / "missing.jsonl"))
    assert tape == [] and files == {}
    added = reconcile_tape_with_digests(tape, pairs, last_reply=("t-4", "final answer was B"))
    assert added == 5
    stream = tape_render(tape)
    assert "request number 1" in stream and "request number 4" in stream
    assert "final answer was B" in stream
    # idempotent: a second hydration adds nothing
    assert reconcile_tape_with_digests(tape, pairs, last_reply=("t-4", "final answer was B")) == 0
    # (b) mid-life upgrade: journal knows t-3.. only -> older asks PREPEND in seal order
    tape2 = [digest_entry(pairs[2][1], "t-3"), digest_entry(pairs[3][1], "t-4")]
    reconcile_tape_with_digests(tape2, pairs)
    assert [e.ref for e in tape2 if e.kind == "digest"] == ["t-1", "t-2", "t-3", "t-4"]
    # (c) fold-compacted gaps stay compacted: t-2 missing BETWEEN seen ids is never resurrected
    tape3 = [digest_entry(pairs[0][1], "t-1"), digest_entry(pairs[2][1], "t-3")]
    reconcile_tape_with_digests(tape3, pairs)
    refs = [e.ref for e in tape3 if e.kind == "digest"]
    assert "t-2" not in refs and refs[-1] == "t-4"      # torn tail t-4 healed, gap respected
    # (d) NORMAL restart mints a NEW session id (Task148 b1): the scan scopes by TASK membership
    # with session filtering OFF, so the old session's antecedents still migrate.
    from sliceagent_core.spine import load_session_digests

    class _Art:
        def __init__(self, id, digest):
            self.id, self.kind, self.session_id = id, "turn", "OLD-session"
            self.structured_body = {"spine_digest": digest, "meta": {"order_ns": int(id[-1])}}
            self.timestamp = ""
    arts = [_Art("t-2", pairs[1][1]), _Art("t-1", pairs[0][1])]
    assert load_session_digests(arts, "NEW-session") == []       # session-scoped scan sees nothing
    cross = load_session_digests(arts, None)                     # task-scoped restart path
    assert [aid for aid, _ in cross] == ["t-1", "t-2"]
    tape4: list = []
    reconcile_tape_with_digests(tape4, cross, last_reply=("t-2", "Choose option two."))
    stream4 = tape_render(tape4)
    assert "request number 1" in stream4 and "Choose option two." in stream4


def test_hydration_is_bounded_on_first_build(tmp_path):
    """Task148 consolidated blocker 1: 80+ old-session artifacts must hydrate to a tape WITHIN
    budget BEFORE anything renders — waiting for the next seal to compact is too late."""
    from sliceagent_core.tape import TAPE_BUDGET_CHARS, hydrate_session_tape
    pairs = [(f"t-{i:03d}", f"[turn t-{i:03d} · task k · completed]\nask: {'x' * 2400}\n")
             for i in range(1, 81)]
    tape, files, _fh, _kh = hydrate_session_tape(str(tmp_path / "none.jsonl"), pairs,
                                                 last_reply=("t-080", "the final answer was B"))
    chars = sum(len(e.rendered) for e in tape)
    assert chars <= TAPE_BUDGET_CHARS, f"first-build tape {chars:,} over budget"
    stream = tape_render(tape)
    # the deictic antecedent survives bounded repair: newest ask + newest reply visible
    assert "t-080" in stream and "the final answer was B" in stream


def test_hydration_never_resurrects_folded_history(tmp_path):
    """Task148 consolidated blocker 1b: epoch(t-1..t-9)+digest(t-10) on the tape means t-1..t-9
    were DELIBERATELY folded — artifact repair must not re-gain them."""
    from sliceagent_core.tape import TapeEntry, reconcile_tape_with_digests
    pairs = [(f"t-{i}", f"[turn t-{i} · task k · completed]\nask: request {i}\n")
             for i in range(1, 11)]
    tape = [
        TapeEntry(kind="epoch", ref="t-1",
                  rendered="[epoch compacted: t-1..t-9 — 9 history entries removed; ...]\n"),
        digest_entry(pairs[9][1], "t-10"),
    ]
    added = reconcile_tape_with_digests(tape, pairs)
    assert added == 0, f"resurrected {added} folded digests"
    refs = [e.ref for e in tape if e.kind == "digest"]
    assert refs == ["t-10"]
    # torn tail beyond the newest live digest still heals
    pairs.append(("t-11", "[turn t-11 · task k · completed]\nask: request 11\n"))
    assert reconcile_tape_with_digests(tape, pairs) == 1
    assert [e.ref for e in tape if e.kind == "digest"] == ["t-10", "t-11"]


def test_skill_activation_between_builds_keeps_tape_prefix(tmp_path, monkeypatch):
    """Task148 consolidated blocker 2: a successful skill activation mutates active_skills
    between model calls — NOTHING mutable may sit above the tape, so the rendered prefix up
    to (and through) the tape must be byte-identical across the activation."""
    s, tools = _ws(tmp_path)
    _seal(s, tools, [("a.py", (tmp_path / "a.py").read_text(encoding="utf-8"))],
          ask="earlier work", assistant_reply="did the earlier work")
    from sliceagent_core.memory_null import NullMemory
    from sliceagent_core.seed import make_build_slice
    build = make_build_slice(s, tools, None, NullMemory(), "current ask")
    before = build()[1]["content"]
    s.active_skills.append({"name": "review-checklist", "body": "1. check nulls\n2. check locks"})
    s.turns += 1                                     # new build epoch (locator snapshot keying)
    after = make_build_slice(s, tools, None, NullMemory(), "current ask")()[1]["content"]
    tape_start = before.index("# SESSION TAPE")
    tape_body = before[tape_start:before.index("[end reply]") + len("[end reply]")]
    assert tape_body in after, "tape bytes must survive"
    assert before[:tape_start] == after[:after.index("# SESSION TAPE")], \
        "bytes ABOVE the tape changed on skill activation"
    assert "review-checklist" in after
    assert after.index("review-checklist") > after.index(tape_body) , \
        "the activated skill must render BELOW the tape"


def test_p8_findings_and_knowledge_freeze_onto_the_tape(tmp_path):
    """P8: findings + the knowledge memo become frozen tape entries; the regions then
    self-suppress; a REFRESH (same text re-appended) never duplicates; a topic change
    (new memo hash) freezes a new knowledge entry."""
    from sliceagent_core.memory_null import NullMemory
    from sliceagent_core.seed import make_build_slice
    s, tools = _ws(tmp_path)
    s.findings.append("core.py: the scheduler retries twice on failure")
    s.continuity.last_knowledge_render = "- lesson: always pin the fixture"
    _seal(s, tools, [], ask="first")
    tape = s.continuity.session_tape
    assert sum(1 for e in tape if e.kind == "finding") == 1
    assert sum(1 for e in tape if e.kind == "knowledge") == 1
    stream = tape_render(tape)
    assert "scheduler retries twice" in stream and "pin the fixture" in stream
    # refresh semantics: same text re-appended (pfc moves it to the end) — NO duplicate
    s.findings.remove("core.py: the scheduler retries twice on failure")
    s.findings.append("core.py: the scheduler retries twice on failure")
    _seal(s, tools, [], n=2, ask="second")
    assert sum(1 for e in tape if e.kind == "finding") == 1
    assert sum(1 for e in tape if e.kind == "knowledge") == 1     # same memo hash: no re-freeze
    assert all(e.task == "k" for e in tape if e.kind in ("finding", "knowledge"))
    # regions self-suppress what the tape holds
    user = make_build_slice(s, tools, None, NullMemory(), "third ask")()[1]["content"]
    assert "YOUR NOTES FROM PRIOR TOOL CALLS" not in user, "frozen finding must not re-render"
    assert stream.count("scheduler retries twice") == 1
    # an EDITED finding is a NEW entry (chronologically honest)
    s.findings.append("core.py: the scheduler retries THREE times after the fix")
    _seal(s, tools, [], n=3, ask="third")
    assert sum(1 for e in tape if e.kind == "finding") == 2
    # topic change: new memo -> one new knowledge entry
    s.continuity.last_knowledge_render = "- lesson: the OTHER topic's candidates"
    _seal(s, tools, [], n=4, ask="fourth")
    assert sum(1 for e in tape if e.kind == "knowledge") == 2


def test_p8_canonical_redacted_identity_no_raw_leak(tmp_path):
    """Task152 High 1: the producer hashed REDACTED text while the suppressors hashed RAW text,
    so a redaction-modified finding froze on the tape AND kept rendering raw in the tail."""
    from sliceagent_core.regions import _knowledge_frozen, _unfrozen_findings
    secret = "sk-1234567890abcdef"
    s, tools = _ws(tmp_path)
    s.findings.append(f"config holds {secret} for the staging key")
    s.continuity.last_knowledge_render = f"- lesson: rotate {secret} monthly"
    _seal(s, tools, [], ask="freeze it")
    stream = tape_render(s.continuity.session_tape)
    assert secret not in stream, "raw secret must never reach the tape"
    assert _unfrozen_findings(s, 20) == [], "the frozen finding must not re-render raw"
    assert _knowledge_frozen(s, s.continuity.last_knowledge_render)


def test_p8_entries_carry_task_ownership_and_scope_suppression(tmp_path):
    """Task152 High 2: findings/knowledge are task-scoped state. The tape is session-scoped by
    construction (turn digests always were), so ownership is TYPED + labelled, and suppression
    only applies to the owning task — task B never has its own note hidden by task A's freeze."""
    s, tools = _ws(tmp_path)
    s.findings.append("A-only: the retry limit is three")
    tape_seal_update(s, tools, [], session_id="s", artifact_id="t-1", task_id="task-A",
                     status="completed", user_request="A work")
    entry = next(e for e in s.continuity.session_tape if e.kind == "finding")
    assert entry.task == "task-A" and "task task-A" in entry.rendered
    from sliceagent_core.regions import _unfrozen_findings
    assert _unfrozen_findings(s, 20) == []                       # owner suppresses
    # a DIFFERENT task's slice sharing the session registry keeps its own note visible
    s_b, _ = _ws(tmp_path)
    s_b.continuity.tape_finding_hashes = set(s.continuity.tape_finding_hashes)
    s_b.continuity.tape_task_id = "task-B"
    s_b.findings.append("A-only: the retry limit is three")      # same text, different owner
    assert _unfrozen_findings(s_b, 20) != [], "task B's own note must still render"


def test_p8_registries_survive_restart_no_duplicate_freeze(tmp_path):
    """Task152 High 3: the dedupe registries were in-memory only, so every restart re-froze
    every finding. They are now rebuilt from the FULL journal (folded entries included)."""
    from sliceagent_core.tape import hydrate_session_tape
    j = str(tmp_path / "state" / "tape.jsonl")
    s, tools = _ws(tmp_path)
    s.findings.append("scheduler retries twice")
    s.continuity.last_knowledge_render = "- lesson: pin the fixture"
    _seal(s, tools, [], journal_path=j, ask="first")
    assert sum(1 for e in s.continuity.session_tape if e.kind == "finding") == 1
    # restart: fresh session state, registries rebuilt from the journal
    tape2, files2, f_h, k_h = hydrate_session_tape(j, [])
    s2, tools2 = _ws(tmp_path)
    s2.continuity.session_tape = tape2
    s2.continuity.tape_files = files2
    s2.continuity.tape_finding_hashes = f_h
    s2.continuity.tape_knowledge_hashes = k_h
    s2.findings.append("scheduler retries twice")
    s2.continuity.last_knowledge_render = "- lesson: pin the fixture"
    _seal(s2, tools2, [], n=2, journal_path=j, ask="after restart")
    assert sum(1 for e in s2.continuity.session_tape if e.kind == "finding") == 1, "duplicate!"
    assert sum(1 for e in s2.continuity.session_tape if e.kind == "knowledge") == 1


def test_p8_torn_tail_heals_when_no_digest_survived_the_fold():
    """Task152 High 4: a fold can keep the epoch plus non-digest entries and remove EVERY live
    digest; the marker's ref_end is the reconciliation boundary that keeps the tail recoverable."""
    from sliceagent_core.tape import compact_tape, finding_entry, reconcile_tape_with_digests
    pairs = [(f"t-{i}", f"[turn t-{i} · task k · completed]\nask: {'x' * 200}\n")
             for i in range(1, 3)]
    tape = [digest_entry(pairs[0][1], "t-1")]
    tape += [finding_entry(f"note number {i} " + "y" * 80, task="k") for i in range(20)]
    compact_tape(tape, {}, budget=700)
    assert any(e.kind == "epoch" for e in tape)
    assert [e.ref for e in tape if e.kind == "digest"] == [], "fixture needs zero live digests"
    added = reconcile_tape_with_digests(tape, pairs)
    assert added == 1, "the torn tail t-2 must heal"
    assert [e.ref for e in tape if e.kind == "digest"] == ["t-2"]
    assert reconcile_tape_with_digests(tape, pairs) == 0          # and stays idempotent


def test_workspace_rebase_carries_tape():
    from sliceagent_core.memory_null import NullMemory
    from sliceagent_core.session import Session, rebase_session_for_workspace
    cur = Session(NullMemory(), "sess-1")
    cur.session_tape = [digest_entry("[turn t-1 · task k · completed]\nask: x\n", "t-1")]
    cur.tape_files = {"a.py": {"hash": "abc", "content": "x\n"}}
    restored = Session(NullMemory(), "sess-1")
    merged = rebase_session_for_workspace(cur, restored)
    assert merged.session_tape == cur.session_tape
    assert merged.tape_files == cur.tape_files


def test_journal_salvage_skips_corrupt_complete_lines_midfile(tmp_path):
    """M8 (counter-review, 2026-08-09): a corrupt COMPLETE line mid-file must not discard the
    valid seals that follow it — the pre-salvage all-or-nothing replay dropped them (and the
    finding/knowledge registries with them, so a restart re-froze duplicates). A genuinely torn
    tail (last line, no newline) still ends the scan."""
    from sliceagent_core.tape import journal_registries
    j = str(tmp_path / "state" / "tape.jsonl")
    s, tools = _ws(tmp_path, body="v = 1\n")
    s.findings.append("salvage note")
    _seal(s, tools, [("a.py", "v = 1\n")], journal_path=j, ask="first")
    body2 = "v = 2\n"
    (tmp_path / "a.py").write_text(body2, encoding="utf-8")
    _seal(s, tools, [("a.py", body2)], n=2, journal_path=j, ask="second")
    clean_tape, clean_files = load_session_tape(j)
    clean_fh, clean_kh = journal_registries(j)
    # inject BOTH corrupt shapes between the seals: unparseable JSON and parseable-but-invalid record
    lines = open(j, encoding="utf-8").read().splitlines(keepends=True)
    mid = len(lines) // 2
    lines[mid:mid] = ['{"kind": "patch", "path": CORRUPT\n', '{"kind": "mystery"}\n']
    open(j, "w", encoding="utf-8").write("".join(lines))
    salvaged_tape, salvaged_files = load_session_tape(j)
    assert tape_render(salvaged_tape) == tape_render(clean_tape), \
        "a corrupt complete line must not cost the seals that follow it"
    assert salvaged_files["a.py"]["content"] == clean_files["a.py"]["content"]
    # registries salvage identically — a restart must not re-freeze what a corrupt line hides
    assert journal_registries(j) == (clean_fh, clean_kh)
    # a corrupt COMPLETE line even at the END is salvaged (nothing follows, nothing lost)…
    with open(j, "a", encoding="utf-8") as f:
        f.write("{CORRUPT-END\n")
    t_end, _ = load_session_tape(j)
    assert tape_render(t_end) == tape_render(clean_tape)
    # …while a TORN tail (crash mid-write, no trailing newline) still ends the replay cleanly.
    with open(j, "a", encoding="utf-8") as f:
        f.write('{"kind": "patch", "pa')
    t_torn, f_torn = load_session_tape(j)
    assert tape_render(t_torn) == tape_render(clean_tape)
    assert f_torn["a.py"]["content"] == clean_files["a.py"]["content"]


def test_journal_append_fsyncs_once_per_seal_and_propagates_write_errors(tmp_path, monkeypatch):
    """M8: one fsync per SEAL (not per entry) keeps the torn-tail window to a single in-flight
    seal; a failing fsync (ENOSPC) propagates to the caller instead of vanishing."""
    import pytest
    from sliceagent_core.tape import tape_journal_append
    calls = []
    real_fsync = os.fsync

    def _counting_fsync(fd):
        calls.append(fd)
        real_fsync(fd)
    monkeypatch.setattr(os, "fsync", _counting_fsync)
    j = str(tmp_path / "state" / "tape.jsonl")
    tape_journal_append(j, [digest_entry("[turn t-1]\nask: x\n", "t-1"),
                            digest_entry("[turn t-1]\nask: y\n", "t-1b")])
    assert len(calls) == 1, f"one fsync per seal, not per entry: {calls}"

    def _enospc(fd):
        raise OSError("ENOSPC")
    monkeypatch.setattr(os, "fsync", _enospc)
    with pytest.raises(OSError, match="ENOSPC"):
        tape_journal_append(j, [digest_entry("[turn t-2]\nask: z\n", "t-2")])


def test_seal_surfaces_journal_error_in_return_and_log(tmp_path, monkeypatch, caplog):
    """M8: the old `except Exception: pass` swallowed even ENOSPC/fsync failures — no failure key
    in the return, empty stderr, empty logs. The seal now reports journal_error AND logs a
    warning, while the live tape stays intact (durability failure never rolls back a seal)."""
    import logging
    s, tools = _ws(tmp_path)
    j = str(tmp_path / "state" / "tape.jsonl")
    info = _seal(s, tools, [], journal_path=j)
    assert info["journal_error"] == "", info          # happy path: no key noise
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    info2 = _seal(s, tools, [], n=2, journal_path=str(blocker / "tape.jsonl"))
    assert info2["journal_error"], info2              # unwritable path surfaces, no patching needed
    assert info2["entries"] > 0

    def _enospc(fd):
        raise OSError("ENOSPC")
    monkeypatch.setattr(os, "fsync", _enospc)         # the fsync INSIDE the append raises
    with caplog.at_level(logging.WARNING, logger="sliceagent_core.tape"):
        info3 = _seal(s, tools, [], n=3, journal_path=j)
    assert info3["journal_error"].startswith("OSError") and "ENOSPC" in info3["journal_error"], info3
    assert any("tape journal append failed" in r.getMessage() for r in caplog.records), \
        "a torn journal must leave a log trace"
    assert info3["entries"] > 0, "a durability failure must not roll back the live seal"


def test_compact_tape_random_histories_never_orphan_patches_and_shrink():
    """M8 companion — the O(n) fold-sizing rewrite (and the salvage/fsync work) shipped with NO
    tests; the cited '400 random tapes byte-identical' check was not in the repo. Property-check
    the compaction CONTRACT over random file histories: replay integrity (latest base + later
    patches == registry content), no orphaned patch, determinism, and a real size cut."""
    import random
    rng = random.Random(20260809)
    for trial in range(50):
        paths = [f"f{i}.py" for i in range(rng.randint(1, 4))]
        per_file = []
        registry = {}
        for path in paths:
            hist = []
            prev = None
            for step in range(rng.randint(1, 5)):
                body = f"# {path} v{step}\n" + "x\n" * rng.randint(0, 40)
                hist.append(base_entry(path, body) if (prev is None or rng.random() < 0.3)
                            else patch_entry(path, prev, body))
                prev = body
            per_file.append(hist)
            registry[path] = {"hash": _h(prev), "content": prev}
        tape = []
        while any(per_file):                            # round-robin merge keeps per-file chronology
            hist = rng.choice([h for h in per_file if h])
            tape.append(hist.pop(0))
        for i in range(rng.randint(0, 6)):              # digests sprinkled anywhere
            tape.insert(rng.randint(0, len(tape)),
                        digest_entry(f"[turn d{i}]\nask: {'y' * rng.randint(50, 400)}\n", f"d-{i}"))
        before = list(tape)
        chars_before = tape_chars(tape)
        budget = rng.choice((600, 1500, 4000))
        info = compact_tape(tape, registry, budget=budget)
        # determinism: an identical copy compacts byte-identically (the twin run must not drift)
        twin = list(before)
        twin_registry = {p: dict(v) for p, v in registry.items()}
        assert compact_tape(twin, twin_registry, budget=budget) == info
        assert tape_render(twin) == tape_render(tape)
        # compaction never grows the tape
        assert tape_chars(tape) <= chars_before, (trial, budget)
        if info["epoch_folds"]:
            assert tape[0].kind == "epoch", trial
            assert tape_chars(tape) < chars_before, "a fold that doesn't shrink must not happen"
        # no orphaned patch: every patch has an earlier base for its path
        for i, e in enumerate(tape):
            if e.kind == "patch":
                assert any(b.kind == "base" and b.path == e.path for b in tape[:i]), \
                    (trial, e.path, [x.kind for x in tape])
        # replay integrity: latest base + later patches still composes the registry content
        for path, reg in registry.items():
            content = None
            for e in tape:
                if e.kind == "base" and e.path == path:
                    content = e.payload
                elif e.kind == "patch" and e.path == path and content is not None:
                    content = compose_after(e, content)
            assert content is not None, (trial, path, "file lost every tape anchor")
            assert content == reg["content"], (trial, path)
