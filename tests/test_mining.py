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
    # (repeated non-failing action signatures) earns the STOP escalation — in both convergence paths.
    s = Slice(); s.reset("t"); s.turn_actions = EXPLORE_NUDGE_AFTER + 6
    s.action_log = {f"read_file:{i}": {"count": 1, "failing": False, "last": "ok"} for i in range(20)}
    out = render_convergence(s)
    assert out and "STOP exploring NOW" not in out, "fresh evidence must not escalate"
    s.action_log["read_file:0"]["count"] = 3
    assert "STOP exploring NOW" in render_convergence(s), "re-observation must escalate"

    s = Slice(); s.reset("t"); s.edited_files = {"a.py"}; s.since_edit = STOP_NUDGE_AFTER + 4
    s.action_log = {f"run_command:{i}": {"count": 1, "failing": False, "last": "ok"} for i in range(10)}
    out = render_convergence(s)
    assert out and "STOP NOW" not in out
    s.action_log["run_command:1"]["count"] = 3
    assert "STOP NOW" in render_convergence(s)


@check
def unverified_frontier_redirects_completion_pressure():
    # #33 P0 (Applied ≠ Verified): with 'ready' items awaiting host verify, convergence must point at
    # earning the receipts; with open items it must name the uncovered scope; never a summary order.
    from types import SimpleNamespace as NS
    s = Slice(); s.reset("t"); s.edited_files = {"a.py"}; s.since_edit = STOP_NUDGE_AFTER + 1
    s.active_work = NS(items=[NS(kind="step", status="ready")])
    out = render_convergence(s)
    assert "awaiting host verification" in out and "final summary" not in out.split("Do NOT")[0]
    s.active_work = NS(items=[NS(kind="step", status="in_progress")])
    out = render_convergence(s)
    assert "still open/in progress" in out and "STOP" not in out
    s.active_work = NS(items=[NS(kind="step", status="verified")])
    assert "Write your final summary" in render_convergence(s), \
        "a fully verified frontier restores the normal completion checkpoint"


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
