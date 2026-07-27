"""Read-only convergence nudge (render_convergence) — the 'show path' spin bug: a read-only task that
keeps exploring without answering gets nudged to answer/ask, but an edit task, a below-threshold turn,
an errored turn, and a delegated explorer do NOT. (The lesson-mining half of this file moved to
test_consolidate.py — distillation is now CACHE-only; the per-turn LessonMiner was removed.)
No model, no pytest. Run: python tests/test_mining.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent.pfc import Slice  # noqa: E402
from sliceagent.regions import STOP_NUDGE_AFTER, EXPLORE_NUDGE_AFTER, render_convergence  # noqa: E402

CHECKS = []
def check(fn):
    CHECKS.append(fn)
    return fn


@check
def readonly_spin_nudges_to_answer():
    s = Slice(); s.reset("show me the path")                       # no edits ever
    s.turn_actions = EXPLORE_NUDGE_AFTER                            # explored this turn without answering
    out = render_convergence(s)
    assert "answer" in out.lower() and ("ask_user" in out or "tool calls this turn" in out)


@check
def readonly_nudge_quiet_below_threshold_and_on_error():
    s = Slice(); s.reset("t")
    s.turn_actions = 2                                            # below EXPLORE_NUDGE_AFTER → no nudge yet
    assert render_convergence(s) == ""
    s.turn_actions = 9; s.last_error = "boom"                     # an error gates the nudge even when explored a lot
    assert render_convergence(s) == ""


@check
def edit_task_uses_postedit_path_not_readonly():
    # once anything is edited, the read-only nudge is dormant — the post-edit convergence path applies
    s = Slice(); s.reset("t"); s.edited_files = {"a.py"}; s.since_edit = STOP_NUDGE_AFTER + 1
    out = render_convergence(s)
    assert "read-only" not in out and "edited 1 file" in out


@check
def explore_mode_suppresses_readonly_nudge():
    # a delegated EXPLORER must NOT be told to stop exploring — its job IS read-only investigation, and the
    # nudge was cutting reviews short before the key (large) files were read. max_steps bounds it instead.
    s = Slice(); s.reset("review the repo"); s.turn_actions = EXPLORE_NUDGE_AFTER + 5
    assert render_convergence(s) != ""        # a normal (top-level) agent WOULD be nudged here
    s.explore_mode = True
    assert render_convergence(s) == ""        # explore_mode suppresses it


@check
def strong_escalation_requires_reobservation_not_raw_count():
    # #33 P0: a long but FRESH evidence trail stays a soft checkpoint; only demonstrated re-checking
    # (same turn, same signature, SAME observation) earns the STOP escalation — in both paths.
    def fresh_calls(n, name="read_file"):
        return [{"id": f"c{i}", "name": name, "args": {"path": f"f{i}"}, "status": "succeeded",
                 "obs_digest": f"d{i}"} for i in range(n)]

    s = Slice(); s.reset("t"); s.turn_actions = EXPLORE_NUDGE_AFTER + 6
    s.runtime.recent_calls = fresh_calls(20)
    # cross-turn history must NOT escalate a fresh turn (#33 amendment): durable repeats are ignored
    s.action_log = {"read_file:old": {"count": 9, "failing": False, "last": "ok"}}
    out = render_convergence(s)
    assert out and "STOP exploring NOW" not in out, "fresh evidence must not escalate"
    # same signature + SAME observation twice = re-checking -> escalate
    s.runtime.recent_calls += [
        {"id": "r1", "name": "read_file", "args": {"path": "f0"}, "status": "succeeded", "obs_digest": "d0"},
        {"id": "r2", "name": "read_file", "args": {"path": "f1"}, "status": "succeeded", "obs_digest": "d1"},
    ]
    assert "STOP exploring NOW" in render_convergence(s), "re-observation must escalate"
    # same signature but CHANGED observation is NEW evidence -> no escalation
    s.runtime.recent_calls = fresh_calls(20) + [
        {"id": "r1", "name": "read_file", "args": {"path": "f0"}, "status": "succeeded", "obs_digest": "new-1"},
        {"id": "r2", "name": "read_file", "args": {"path": "f1"}, "status": "succeeded", "obs_digest": "new-2"},
    ]
    assert "STOP exploring NOW" not in render_convergence(s)

    s = Slice(); s.reset("t"); s.edited_files = {"a.py"}; s.since_edit = STOP_NUDGE_AFTER + 4
    s.runtime.recent_calls = fresh_calls(10, name="run_command")
    out = render_convergence(s)
    assert out and "STOP NOW" not in out
    s.runtime.recent_calls += [
        {"id": "r1", "name": "run_command", "args": {"path": "f0"}, "status": "succeeded", "obs_digest": "d0"},
        {"id": "r2", "name": "run_command", "args": {"path": "f1"}, "status": "succeeded", "obs_digest": "d1"},
    ]
    assert "STOP NOW" in render_convergence(s)


@check
def observation_digest_covers_full_output():
    # #33 amendment: identical 4096-char prefixes with changed tails are NEW evidence — the digest
    # hashes the full output, and repeat pressure only counts unchanged observations.
    from sliceagent.events import ToolResult
    from sliceagent.regions import _repeat_pressure
    from sliceagent.slice_reducer import SliceReducer
    s = Slice(); s.reset("t")
    red = SliceReducer(s)
    base = "x" * 5000
    red(ToolResult("grep", {"q": "a"}, base + "TAIL-ONE", False, invocation_id="c1"))
    red(ToolResult("grep", {"q": "a"}, base + "TAIL-TWO", False, invocation_id="c2"))
    d1, d2 = (c.get("obs_digest") for c in s.runtime.recent_calls)
    assert d1 and d2 and d1 != d2, "changed tail past the old 4096 prefix must change the digest"
    assert _repeat_pressure(s) == 0
    red(ToolResult("grep", {"q": "a"}, base + "TAIL-TWO", False, invocation_id="c3"))
    assert _repeat_pressure(s) == 1, "unchanged observation repeated = re-checking"


@check
def unverified_frontier_redirects_completion_pressure():
    # #33 P0 (Applied != Verified): ready items redirect to earning receipts; open items are named
    # scope; waiting_user (incl. the REQUEST ROOT itself) is a parked human dependency; stale items
    # under an older resolved root must not hijack the current request.
    from types import SimpleNamespace as NS

    def edited():
        s = Slice(); s.reset("t"); s.edited_files = {"a.py"}; s.since_edit = STOP_NUDGE_AFTER + 1
        return s

    s = edited(); s.active_work = NS(items=[NS(kind="step", status="ready", root_id="r1")],
                                     unresolved_roots=[NS(id="r1", status="open")])
    out = render_convergence(s)
    assert "awaiting host verification" in out
    s = edited(); s.active_work = NS(items=[NS(kind="step", status="in_progress", root_id="r1")],
                                     unresolved_roots=[NS(id="r1", status="open")])
    out = render_convergence(s)
    assert "still open/in progress" in out and "STOP" not in out
    # waiting child -> parked, not "advance now"
    s = edited(); s.active_work = NS(items=[NS(kind="step", status="waiting_user", root_id="r1")],
                                     unresolved_roots=[NS(id="r1", status="open")])
    out = render_convergence(s)
    assert "waiting on the USER" in out and "advance" not in out
    # ROOT-ONLY parked turn: request root itself waiting_user, no waiting child (#33 amendment)
    s = edited(); s.active_work = NS(items=[], unresolved_roots=[NS(id="r1", status="waiting_user")])
    out = render_convergence(s)
    assert "waiting on the USER" in out
    # two-root: stale ready item under an older root must NOT hijack the current root's convergence
    s = edited(); s.active_work = NS(
        items=[NS(kind="step", status="ready", root_id="r1"),
               NS(kind="step", status="verified", root_id="r2")],
        unresolved_roots=[NS(id="r2", status="open")])
    assert "Write your final summary" in render_convergence(s), \
        "an older root's stale 'ready' item hijacked the current request's convergence"


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
