"""Typed execution-kernel invariants. No network/model dependency."""
from __future__ import annotations

import os
import signal
import shlex
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent.events import (ApiRetry, AssistantText, ToolResult, TurnEnd,
                               TurnInterrupted)  # noqa: E402
from sliceagent.execution import (CHILD_ACTIVITY_ARG, CHILD_INVOCATION_ID_ARG,
                                  CHILD_REQUEST_ORDINAL_ARG, ChildActivity, PreflightOverflow, ToolEffect,
                                  ToolInvocation, ToolOutcome, ToolPurity, ToolStatus, TurnOutcome,
                                  Usage, preflight_model_call, reconciliation_targets)  # noqa: E402
from sliceagent.hooks import BudgetHook, Hooks  # noqa: E402
from sliceagent.loop import (_assistant_message, _delegation_absolute, _delegation_timeout,
                             run_tool_batch, run_turn)  # noqa: E402
from sliceagent.model_runner import complete_model_call  # noqa: E402
from sliceagent.registry import ToolEntry, ToolRegistry, ToolText  # noqa: E402
from sliceagent.scheduler import ScheduledTool, run_ordered  # noqa: E402


CHECKS = []

# Every duration in this file goes through T() — see tests/_timescale.py for why partial scaling
# is worse than none, and why the scheduler's own constants have to stretch with it.
from _timescale import T  # noqa: E402


def check(fn):
    CHECKS.append(fn)
    return fn


def _tc(name, args, call_id):
    return NS(name=name, args=args, id=call_id)


@check
def tool_bearing_assistant_prose_is_presentation_only():
    response = NS(
        content="I will do two waves and the report is above.",
        reasoning_content="provider replay token",
        tool_calls=[_tc("spawn_agent", {"agent": "explorer", "task": "one"}, "child-1")],
    )
    message = _assistant_message(response)
    assert message["content"] == ""
    assert message["reasoning_content"] == "provider replay token"
    assert message["tool_calls"][0]["id"] == "child-1"


@check
def settled_multi_child_batch_returns_ordered_full_reports_directly():
    class LLM:
        def __init__(self):
            self.calls = 0
            self.synthesis_messages = []

        def complete(self, messages, _schemas):
            self.calls += 1
            if self.calls == 1:
                return NS(
                    content="launching two waves; preliminary findings above",
                    tool_calls=[
                        _tc("spawn_agent", {"agent": "explorer", "task": "one"}, "child-1"),
                        _tc("spawn_agent", {"agent": "explorer", "task": "two"}, "child-2"),
                    ],
                    finish_reason="tool_calls", usage={},
                )
            self.synthesis_messages = list(messages)
            return NS(content="final synthesis", tool_calls=[], finish_reason="stop", usage={})

    class Host:
        def schemas(self):
            return []

        def accesses(self, _name, _args):
            from sliceagent.access import ReadAllAccess
            return [ReadAllAccess()]

        def run(self, _name, args):
            index = 1 if args["task"] == "one" else 2
            artifact_id = f"artifact-{index}"
            report = f"BEGIN CHILD REPORT {index}\n" + ("x" * 1200) + f"\nFULL CHILD {index} MIDDLE"
            return ToolText(
                report,
                effects=(ToolEffect(
                    f"{artifact_id}:outcome", "child_outcome", {
                        "artifact_id": artifact_id,
                        "operational_status": "succeeded",
                        "source_coverage_status": "source_complete",
                        "explorer_evidence_status": "content_retained",
                    },
                ),),
            )

    llm = LLM()
    events = []
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "review everything"}],
        llm=llm, tools=Host(), dispatch=events.append, hooks=Hooks(), max_steps=3,
    )
    assert result.stop_reason == "end_turn" and llm.calls == 2
    trajectory = llm.synthesis_messages[1:]
    assert [message["role"] for message in trajectory] == ["assistant", "tool", "tool"]
    assert trajectory[0]["content"] == ""
    assistant_ids = [call["id"] for call in trajectory[0]["tool_calls"]]
    assert [message["tool_call_id"] for message in trajectory[1:]] == assistant_ids
    assert "FULL CHILD 1 MIDDLE" in trajectory[1]["content"]
    assert "FULL CHILD 2 MIDDLE" in trajectory[2]["content"]
    rendered = "\n".join(str(message.get("content") or "") for message in trajectory)
    assert "HOST FAN-IN" not in rendered and "preliminary findings above" not in rendered
    finals = [event for event in events if isinstance(event, AssistantText) and event.final]
    assert len(finals) == 1 and finals[0].content == "final synthesis"


@check
def indeterminate_child_does_not_hide_settled_sibling_report():
    class LLM:
        def __init__(self):
            self.calls = 0
            self.closeout_messages = []

        def complete(self, messages, schemas):
            self.calls += 1
            if self.calls == 1:
                return NS(
                    content="",
                    tool_calls=[
                        _tc("spawn_agent", {"agent": "explorer", "task": "settled"}, "child-ok"),
                        _tc("spawn_agent", {"agent": "explorer", "task": "uncertain"}, "child-unknown"),
                    ],
                    finish_reason="tool_calls", usage={},
                )
            self.closeout_messages = list(messages)
            assert schemas == [], "an indeterminate wave permits synthesis, not another effectful tool"
            return NS(
                content="Child one found the retained issue; child two remains indeterminate.",
                tool_calls=[], finish_reason="stop", usage={},
            )

    class Host:
        def schemas(self):
            return []

        def accesses(self, _name, _args):
            from sliceagent.access import ReadAllAccess
            return [ReadAllAccess()]

        def run(self, _name, args):
            if args["task"] == "settled":
                report = "BEGIN CHILD REPORT\nCONFIRMED SETTLED FINDING\nEND CHILD REPORT"
                return ToolText(report, effects=(ToolEffect(
                    "settled:outcome", "child_outcome", {
                        "status": "succeeded",
                        "operational_status": "succeeded",
                        "kind": "explorer",
                        "launch_ordinal": 1,
                        "report_completion": "complete",
                        "report_bytes": len("CONFIRMED SETTLED FINDING"),
                        "report_sha256": "a" * 64,
                        "report_handle": "artifacts/settled.md",
                    },
                ),))
            return ToolText(
                "Error: child provider state is unresolved",
                status=ToolStatus.INDETERMINATE,
                effects=(ToolEffect(
                    "unknown:outcome", "child_outcome", {
                        "status": "indeterminate",
                        "operational_status": "indeterminate",
                        "kind": "explorer",
                        "launch_ordinal": 2,
                        "report_completion": "absent",
                        "report_bytes": 0,
                    },
                ),),
            )

    llm, events = LLM(), []
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "review both areas"}],
        llm=llm, tools=Host(), dispatch=events.append, hooks=Hooks(), max_steps=3,
    )
    assert result.stop_reason == "indeterminate"
    assert llm.calls == 2, "the only follow-up is one synthesis-only closeout"
    assert any(
        "CONFIRMED SETTLED FINDING" in str(message.get("content") or "")
        for message in llm.closeout_messages
    ), "the settled sibling's full direct report must reach synthesis"
    updates = [event.content for event in events
               if isinstance(event, AssistantText) and not event.final]
    assert "Child one found the retained issue; child two remains indeterminate." in updates
    assert not any(isinstance(event, TurnEnd) for event in events)
    interrupts = [event for event in events if isinstance(event, TurnInterrupted)]
    assert len(interrupts) == 1 and "artifacts/settled.md" in (interrupts[0].message or "")


@check
def lifecycle_child_wave_caps_parallel_full_model_loops_at_four():
    lock = threading.Lock()
    release = threading.Event()
    four_running = threading.Event()
    state = {"active": 0, "maximum": 0}

    def task(index):
        invocation = ToolInvocation(f"child-{index}", "spawn_agent", {}, index)

        def run():
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                if state["active"] >= 4:
                    four_running.set()
            assert release.wait(T(2))
            with lock:
                state["active"] -= 1
            return ToolOutcome(invocation, ToolStatus.SUCCEEDED, "done")

        return ScheduledTool(
            invocation, ToolPurity.PURE_READ, run, timeout_safe=False,
        )

    box = {}
    runner = threading.Thread(
        target=lambda: box.setdefault("outcomes", run_ordered([task(i) for i in range(7)])),
        daemon=True,
    )
    runner.start()
    assert four_running.wait(T(1))
    time.sleep(T(0.05))  # give an incorrectly uncapped wave ample time to launch children 5-7
    assert state["maximum"] == 4, state
    release.set()
    runner.join(T(2))
    assert not runner.is_alive()
    assert len(box["outcomes"]) == 7


@check
def indeterminate_lifecycle_child_cancels_only_the_unadmitted_wave_tail():
    lock = threading.Lock()
    four_running = threading.Event()
    release_started = threading.Event()
    started = []

    def task(index):
        invocation = ToolInvocation(f"uncertain-child-{index}", "spawn_agent", {}, index)

        def run():
            with lock:
                started.append(index)
                if len(started) == 4:
                    four_running.set()
            assert four_running.wait(T(2))
            if index == 0:
                return ToolOutcome(
                    invocation, ToolStatus.INDETERMINATE,
                    "provider watchdog expired; request may still be in flight",
                )
            assert release_started.wait(T(2))
            return ToolOutcome(invocation, ToolStatus.SUCCEEDED, "settled")

        return ScheduledTool(invocation, ToolPurity.PURE_READ, run, timeout_safe=False)

    box = {}
    runner = threading.Thread(
        target=lambda: box.setdefault("outcomes", run_ordered([task(i) for i in range(6)])),
        daemon=True,
    )
    runner.start()
    try:
        assert four_running.wait(T(1))
        time.sleep(T(0.12))  # allow the scheduler to observe child 0 and close the queued tail
        with lock:
            assert sorted(started) == [0, 1, 2, 3], started
        release_started.set()
        runner.join(T(2))
        assert not runner.is_alive()
        outcomes = box["outcomes"]
        assert [outcome.status for outcome in outcomes] == [
            ToolStatus.INDETERMINATE,
            ToolStatus.SUCCEEDED,
            ToolStatus.SUCCEEDED,
            ToolStatus.SUCCEEDED,
            ToolStatus.CANCELLED,
            ToolStatus.CANCELLED,
        ]
        expected = (
            "Not run: an earlier invocation in this wave has an unresolved outcome; "
            "queued execution was not admitted"
        )
        assert [outcome.text for outcome in outcomes[4:]] == [expected, expected]
    finally:
        release_started.set()
        runner.join(T(1))


@check
def failed_lifecycle_child_still_admits_queued_wave_siblings():
    started = []

    def task(index):
        invocation = ToolInvocation(f"failed-child-{index}", "spawn_agent", {}, index)

        def run():
            started.append(index)
            status = ToolStatus.FAILED if index == 0 else ToolStatus.SUCCEEDED
            return ToolOutcome(invocation, status, "settled")

        return ScheduledTool(invocation, ToolPurity.PURE_READ, run, timeout_safe=False)

    outcomes = run_ordered([task(i) for i in range(3)], max_workers=1)
    assert started == [0, 1, 2]
    assert [outcome.status for outcome in outcomes] == [
        ToolStatus.FAILED, ToolStatus.SUCCEEDED, ToolStatus.SUCCEEDED,
    ]


@check
def delegation_timeout_cannot_be_disabled_with_nonfinite_values():
    old = os.environ.get("AGENT_DELEGATION_TIMEOUT")
    try:
        for raw in ("inf", "-inf", "nan", "1e309", "0", "invalid"):
            os.environ["AGENT_DELEGATION_TIMEOUT"] = raw
            assert _delegation_timeout() == 900.0, raw
        os.environ["AGENT_DELEGATION_TIMEOUT"] = "12.5"
        assert _delegation_timeout() == 12.5
    finally:
        if old is None:
            os.environ.pop("AGENT_DELEGATION_TIMEOUT", None)
        else:
            os.environ["AGENT_DELEGATION_TIMEOUT"] = old


@check
def delegation_absolute_cannot_be_disabled_with_nonfinite_values():
    old = os.environ.get("AGENT_DELEGATION_ABSOLUTE")
    try:
        for raw in ("inf", "-inf", "nan", "1e309", "0", "-1", "invalid"):
            os.environ["AGENT_DELEGATION_ABSOLUTE"] = raw
            assert _delegation_absolute() == 3600.0, raw
        os.environ["AGENT_DELEGATION_ABSOLUTE"] = "12.5"
        assert _delegation_absolute() == 12.5
    finally:
        if old is None:
            os.environ.pop("AGENT_DELEGATION_ABSOLUTE", None)
        else:
            os.environ["AGENT_DELEGATION_ABSOLUTE"] = old


@check
def child_inactivity_timeout_is_per_job_not_pooled_by_a_live_sibling():
    inactive_cancel = threading.Event()
    live_cancel = threading.Event()
    inactive_activity = ChildActivity()
    live_activity = ChildActivity()
    inactive_inv = ToolInvocation("inactive-child", "spawn_agent", {}, 0)
    live_inv = ToolInvocation("live-child", "spawn_agent", {}, 1)

    def inactive():
        assert inactive_cancel.wait(T(1))
        return ToolOutcome(inactive_inv, ToolStatus.CANCELLED, "inactive child closed")

    def live():
        until = time.monotonic() + T(0.18)
        while time.monotonic() < until:
            assert not live_cancel.is_set(), "live child was cancelled by its sibling's inactivity"
            live_activity.touch()
            time.sleep(T(0.01))
        return ToolOutcome(live_inv, ToolStatus.SUCCEEDED, "live child completed")

    outcomes = run_ordered([
        ScheduledTool(
            inactive_inv, ToolPurity.PURE_READ, inactive, timeout_safe=False,
            request_cancel=lambda _kind: inactive_cancel.set(), cancel_grace=T(0.2),
            activity=inactive_activity,
        ),
        ScheduledTool(
            live_inv, ToolPurity.PURE_READ, live, timeout_safe=False,
            request_cancel=lambda _kind: live_cancel.set(), cancel_grace=T(0.2),
            activity=live_activity,
        ),
    ], lifecycle_timeout=T(0.05), lifecycle_absolute=T(1.0))
    assert [outcome.status for outcome in outcomes] == [
        ToolStatus.FAILED, ToolStatus.SUCCEEDED,
    ], outcomes
    assert f"no activity for {T(0.05):g}s" in outcomes[0].text
    assert not live_cancel.is_set()


@check
def active_child_still_stops_at_absolute_delegation_guard():
    cancel = threading.Event()
    activity = ChildActivity()
    invocation = ToolInvocation("long-child", "spawn_agent", {}, 0)

    def active():
        while not cancel.wait(T(0.01)):
            activity.touch()
        return ToolOutcome(invocation, ToolStatus.CANCELLED, "active child closed")

    outcomes = run_ordered([
        ScheduledTool(
            invocation, ToolPurity.PURE_READ, active, timeout_safe=False,
            request_cancel=lambda _kind: cancel.set(), cancel_grace=T(0.2), activity=activity,
        ),
    ], lifecycle_timeout=T(0.05), lifecycle_absolute=T(0.12))
    assert outcomes[0].status is ToolStatus.FAILED
    assert f"{T(0.12):g}s absolute delegation leak guard" in outcomes[0].text


@check
def omitted_lifecycle_absolute_retains_the_scheduler_leak_guard():
    """The public scheduler API must fail closed even when an embedder omits the override.

    Before #59, ``run_ordered(..., lifecycle_timeout=...)`` forwarded ``None`` to the
    per-job wave. A child that kept touching its activity cell could then run forever.
    """
    import sliceagent.scheduler as scheduler

    cancel = threading.Event()
    activity = ChildActivity()
    invocation = ToolInvocation("default-absolute-child", "spawn_agent", {}, 0)

    def active():
        while not cancel.wait(T(0.01)):
            activity.touch()
        return ToolOutcome(invocation, ToolStatus.CANCELLED, "active child closed")

    prior = scheduler.DEFAULT_LIFECYCLE_ABSOLUTE
    scheduler.DEFAULT_LIFECYCLE_ABSOLUTE = T(0.12)
    try:
        outcomes = run_ordered([
            ScheduledTool(
                invocation,
                ToolPurity.PURE_READ,
                active,
                timeout_safe=False,
                request_cancel=lambda _kind: cancel.set(),
                cancel_grace=T(0.2),
                activity=activity,
            ),
        ], lifecycle_timeout=T(0.05))
    finally:
        scheduler.DEFAULT_LIFECYCLE_ABSOLUTE = prior

    assert outcomes[0].status is ToolStatus.FAILED
    assert f"{T(0.12):g}s absolute delegation leak guard" in outcomes[0].text


@check
def wedged_children_over_the_wave_ceiling_cannot_freeze_the_parent_turn():
    """Every liveness test above uses 1-2 children, so none of them ever fills the wave ceiling —
    which is exactly where the per-job path could hang. With more lifecycle children than
    _MAX_PARALLEL_LIFECYCLE_WAVE, children that ignore their cancellation lease hold every worker
    slot; the launch pointer then freezes below len(jobs). Gating the settlement exit on that pointer
    made both exits unreachable and (since per_job_liveness sets the wave deadline to None) the wave
    spun at the poll interval forever, past the absolute leak guard that exists to prevent precisely
    this. Measured: >20s and still spinning before the fix, 0.71s after."""
    from sliceagent.scheduler import _MAX_PARALLEL_LIFECYCLE_WAVE
    total = _MAX_PARALLEL_LIFECYCLE_WAVE + 1
    wedge = threading.Event()
    tasks = []
    for index in range(total):
        invocation = ToolInvocation(f"wedged-{index}", "spawn_agent", {}, index)
        tasks.append(ScheduledTool(
            invocation, ToolPurity.PURE_READ,
            lambda: (wedge.wait(T(30)), "never")[1],      # never returns: slot.release() never runs
            timeout_safe=False,
            request_cancel=lambda _kind: None,          # accepts the lease, ignores it
            cancel_grace=T(0.05), activity=ChildActivity(),
        ))

    box: dict = {}
    runner = threading.Thread(
        target=lambda: box.setdefault(
            "outcomes", run_ordered(tasks, lifecycle_timeout=T(0.3), lifecycle_absolute=T(0.6))),
        daemon=True,
    )
    runner.start()
    runner.join(T(15.0))
    frozen = runner.is_alive()
    wedge.set()                                          # let the daemon threads unwind either way
    assert not frozen, (
        "the wave never returned: wedged children filling the ceiling froze the parent turn, and the "
        "absolute leak guard could not break it"
    )
    outcomes = box["outcomes"]
    assert len(outcomes) == total, outcomes
    # EXACT statuses (#55): a reaped-but-unconfirmed child is INDETERMINATE — never FAILED, which
    # would fabricate a verdict about work whose physical state is unknown (NO VERDICT != FAILED).
    # The earlier `in (INDETERMINATE, CANCELLED, FAILED)` tolerance let that mutation pass green.
    assert all(o.status is ToolStatus.INDETERMINATE for o in outcomes[:-1]), \
        [o.status for o in outcomes]
    assert outcomes[-1].status is ToolStatus.CANCELLED, "the queued tail never ran"
    # ...and the reap is TYPED, not prose: every wedged child's effects carry its timeout_kind.
    for out in outcomes[:-1]:
        kinds = [e.payload.get("timeout_kind") for e in out.effects
                 if isinstance(getattr(e, "payload", None), dict) and "timeout_kind" in e.payload]
        assert kinds and all(k in ("inactivity", "absolute") for k in kinds), \
            f"wedged child lost its typed timeout_kind: {out.text[:80]!r} {kinds}"


@check
def production_loop_forwards_the_absolute_guard_to_the_scheduler():
    """#55: deleting the production `lifecycle_absolute=_delegation_absolute()` wiring kept the
    suite green — every test passed the guard by hand. Pin the forwarding and the guard's
    fail-closed parsing so the leak guard cannot silently vanish from the production path."""
    import inspect as _inspect
    import os as _os

    from sliceagent import loop as _loop
    src = _inspect.getsource(_loop.run_tool_batch)
    assert "lifecycle_absolute=_delegation_absolute()" in src, \
        "run_tool_batch no longer forwards the absolute leak guard to run_ordered"
    prior = _os.environ.get("AGENT_DELEGATION_ABSOLUTE")
    try:
        for raw, want in (("junk", 3600.0), ("0", 3600.0), ("-5", 3600.0), ("1800", 1800.0)):
            _os.environ["AGENT_DELEGATION_ABSOLUTE"] = raw
            assert _loop._delegation_absolute() == want, (raw, _loop._delegation_absolute())
    finally:
        _os.environ.pop("AGENT_DELEGATION_ABSOLUTE", None)
        if prior is not None:
            _os.environ["AGENT_DELEGATION_ABSOLUTE"] = prior


@check
def per_job_reap_outranks_a_later_parent_cancel_in_assembly():
    """Two-child race (lexie's counterexample on 113ec74): child A is reaped by its inactivity
    window and then SETTLES while child B is still live; the parent then cancels the wave. A's
    earlier per-job timeout is a recorded fact — assembly must keep its delegation-timeout
    classification and typed timeout_kind, not overwrite it with plain parent-cancel just because
    the wave-level cutoff_kind is 'cancel'."""
    a_inv = ToolInvocation("reaped-then-settled", "spawn_agent", {}, 0)
    b_inv = ToolInvocation("live-sibling", "spawn_agent", {}, 1)
    a_activity, b_activity = ChildActivity(), ChildActivity()
    cancel_at = time.monotonic() + T(0.7)

    def child_a():
        time.sleep(T(0.5))                      # silent past the 0.3s window -> reaped; settles inside grace
        return ToolOutcome(a_inv, ToolStatus.SUCCEEDED, "late but present")

    def child_b():
        until = time.monotonic() + T(1.4)
        while time.monotonic() < until:
            b_activity.touch()               # provably live the whole time
            time.sleep(T(0.02))
        return ToolOutcome(b_inv, ToolStatus.SUCCEEDED, "b done")

    tasks = [
        ScheduledTool(a_inv, ToolPurity.PURE_READ, child_a, timeout_safe=False,
                      request_cancel=lambda _k: None, cancel_grace=T(0.4), activity=a_activity),
        ScheduledTool(b_inv, ToolPurity.PURE_READ, child_b, timeout_safe=False,
                      request_cancel=lambda _k: None, cancel_grace=T(0.4), activity=b_activity),
    ]
    outcomes = run_ordered(tasks, lifecycle_timeout=T(0.3), lifecycle_absolute=T(5.0),
                           should_cancel=lambda: time.monotonic() >= cancel_at)
    assert len(outcomes) == 2
    a_out = outcomes[0]
    kinds = [e.payload.get("timeout_kind") for e in a_out.effects
             if isinstance(getattr(e, "payload", None), dict) and "timeout_kind" in e.payload]
    assert "inactivity" in kinds, (
        f"A's recorded per-job reap was overwritten by the later wave cancel: status={a_out.status} "
        f"text={a_out.text[:120]!r} effect_kinds={kinds}")
    assert "no activity" in a_out.text, a_out.text[:160]


@check
def a_queued_steer_cuts_the_wave_early_and_keeps_every_report():
    """FIELD REGRESSION: a steer typed during a 5-child fan-out had no drain point inside the batch,
    so it waited for every child (measured 15+ min in live use). The scheduler's steer_probe cuts the
    wave at its next boundary with kind "steer": in-flight children get their cancel lease + full seal
    grace, the queued tail is abandoned as CANCELLED (↷ — a redirect, never ✗), and every report that
    already completed survives untouched."""
    fast_inv = ToolInvocation("fast-child", "spawn_agent", {}, 0)
    partial_inv = ToolInvocation("partial-child", "spawn_agent", {}, 1)
    quiet_inv = ToolInvocation("quiet-child", "spawn_agent", {}, 2)
    start = time.monotonic()

    def fast():
        return ToolOutcome(fast_inv, ToolStatus.SUCCEEDED, "full report A")

    def blocking(inv, report):
        lease = threading.Event()

        def run():
            deadline = start + 30                       # natural settle: 30s from now
            while time.monotonic() < deadline and not lease.is_set():
                time.sleep(T(0.02))
            if report:
                # the child sealed a partial report inside its cancel grace — typed, kept
                return ToolOutcome(inv, ToolStatus.SUCCEEDED, f"[partial] {report}")
            return ToolOutcome(inv, ToolStatus.CANCELLED,
                             "Not run to completion: the delegation was cancelled")
        return run, lease

    partial_run, partial_lease = blocking(partial_inv, "half findings B")
    quiet_run, quiet_lease = blocking(quiet_inv, "")
    tasks = [
        ScheduledTool(fast_inv, ToolPurity.PURE_READ, fast, timeout_safe=False,
                      request_cancel=lambda _kind: None, cancel_grace=T(0.2)),
        ScheduledTool(partial_inv, ToolPurity.PURE_READ, partial_run, timeout_safe=False,
                      request_cancel=lambda _kind: partial_lease.set(), cancel_grace=T(0.2)),
        ScheduledTool(quiet_inv, ToolPurity.PURE_READ, quiet_run, timeout_safe=False,
                      request_cancel=lambda _kind: quiet_lease.set(), cancel_grace=T(0.2)),
    ]
    probe_at = start + 0.4
    outcomes = run_ordered(tasks, lifecycle_timeout=T(30.0), lifecycle_absolute=T(60.0),
                           steer_probe=lambda: time.monotonic() >= probe_at)
    elapsed = time.monotonic() - start
    assert elapsed < 10, f"the steer was held hostage by the wave for {elapsed:.1f}s (natural: 30s)"
    assert len(outcomes) == 3
    assert "full report A" in outcomes[0].text, "a completed report must survive the cutoff"
    assert "half findings B" in outcomes[1].text, (
        f"a steer-cancelled child's sealed partial report was discarded: {outcomes[1].text[:100]!r}")
    assert outcomes[2].status is ToolStatus.CANCELLED, outcomes[2].status
    for out in outcomes:
        assert out.status is not ToolStatus.FAILED, (
            f"a steer is a redirect — nothing may paint ✗: {out.status} {out.text[:80]!r}")


@check
def the_wave_runs_to_natural_settle_without_a_steer_probe():
    """The negative-control shape: with no probe wired, the same wave holds every child to natural
    completion — the pre-fix behaviour the probe exists to eliminate."""
    inv = ToolInvocation("slow-child", "spawn_agent", {}, 0)
    start = time.monotonic()

    def slow():
        time.sleep(T(1.2))
        return ToolOutcome(inv, ToolStatus.SUCCEEDED, "slow done")

    outcomes = run_ordered([
        ScheduledTool(inv, ToolPurity.PURE_READ, slow, timeout_safe=False,
                      request_cancel=lambda _kind: None, cancel_grace=T(0.1)),
    ], lifecycle_timeout=T(30.0), lifecycle_absolute=T(60.0))
    assert time.monotonic() - start >= 1.2 and outcomes[0].status is ToolStatus.SUCCEEDED


@check
def a_prompting_command_fails_fast_instead_of_hanging_to_the_deadline():
    """FIELD: `npx tsc --noEmit && npm run build` sat at 0% CPU for 4+ minutes — a child in the
    chain was reading the INHERITED TTY stdin, invisible behind the TUI, until the deadline reaped
    it. One-shot runs have no stdin BY CONTRACT (terminal.py says so, procman enforces it): DEVNULL
    makes a prompt fail fast and readably, and stops the child competing with the TUI."""
    from sliceagent.tools import LocalToolHost

    with tempfile.TemporaryDirectory(prefix="stdin-contract-") as root:
        host = LocalToolHost(root=root)
        start = time.monotonic()
        out = host.run("run_command", {"command": "read -r answer", "timeout": 10})
        elapsed = time.monotonic() - start
        assert elapsed < 8, f"a prompt hung for {elapsed:.1f}s — stdin is still being inherited"
        assert "timed out" not in str(out).lower(), str(out)[:160]
        assert "ok" in host.run("run_command", {"command": "echo ok"}), "ordinary commands unaffected"
        if os.name != "nt":
            # deterministic pin: the child's fd 0 must BE /dev/null. Point the PARENT's fd 0 at a
            # real file first, so an inheriting child provably sees NOT-/dev/null regardless of what
            # the harness's own stdin happens to be.
            with open(os.path.join(root, "probe_stdin.py"), "w", encoding="utf-8") as f:
                f.write("import os\n"
                        "print('NULL' if os.fstat(0).st_rdev == os.stat('/dev/null').st_rdev "
                        "else 'OTHER')\n")
            saved_fd = os.dup(0)
            real_file = os.open(os.path.join(root, "probe_stdin.py"), os.O_RDONLY)
            os.dup2(real_file, 0)
            try:
                out = host.run("run_command", {"command": "python3 probe_stdin.py"})
                assert "NULL" in str(out), str(out)[:120]
                out = host.run("execute_code", {"code": "print(run('python3 probe_stdin.py'))"})
                assert "NULL" in str(out), f"the execute_code prelude inherits stdin too: {str(out)[:160]}"
            finally:
                os.dup2(saved_fd, 0)
                os.close(saved_fd)
                os.close(real_file)


@check
def the_leak_guard_wait_does_not_busy_spin_once_it_elapses():
    """REGRESSION: clamping the poll to a deadline already in the PAST pins wait_for to 0.0, so
    condition.wait(T(0.0)) spins on the scheduler lock until the wave breaks — and that spin starves the
    worker trying to TAKE the lock to publish its settlement, flipping typed closes to INDETERMINATE.
    Measured 0.104s CPU vs 0.005s for the same 0.9s of wall. Assert the RATIO, not a wall-clock."""
    wedge = threading.Event()

    def task(i):
        inv = ToolInvocation(f"spin-{i}", "spawn_agent", {}, i)
        return ScheduledTool(inv, ToolPurity.PURE_READ, lambda: (wedge.wait(T(20)), "x")[1],
                             timeout_safe=False, request_cancel=lambda _k: None,
                             cancel_grace=T(0.05), activity=ChildActivity())

    box: dict = {}
    # process_time() is user+system CPU for the whole process — the same quantity as
    # ru_utime + ru_stime, minus the POSIX-only `resource` import that broke the Windows job.
    before = time.process_time()
    started = time.monotonic()
    runner = threading.Thread(target=lambda: box.setdefault(
        "o", run_ordered([task(i) for i in range(4)],
                         lifecycle_timeout=None, lifecycle_absolute=T(0.5))), daemon=True)
    runner.start()
    runner.join(T(12.0))
    alive = runner.is_alive()
    wedge.set()
    wall = time.monotonic() - started
    cpu = time.process_time() - before
    assert not alive, "the leak guard never released the wave"
    # A polling wait costs almost nothing; a spin costs a large fraction of the wall clock.
    # Measured on this path: waiting ~0.5% of wall, spinning ~11%. 4% sits an order of magnitude
    # above the wait and well under the spin. (My first threshold was 25% and the pin passed with the
    # fix reverted — a bound loose enough to admit the bug is not a pin.)
    assert cpu < wall * 0.04, (
        f"the wave burned {cpu:.3f}s CPU over {wall:.2f}s wall ({cpu / wall:.1%}) — the poll is "
        "spinning on the scheduler lock, not waiting on it"
    )


@check
def the_absolute_leak_guard_holds_without_an_inactivity_window():
    """M-s1: `lifecycle_timeout` defaults to None in run_ordered's own signature, and the absolute
    leak guard was gated on `per_job_liveness`, which REQUIRES a non-None timeout. So a caller using
    the documented default got neither bound and one non-terminating child blocked the parent
    forever. The guard exists for exactly the case where nothing else bounds the child."""
    wedge = threading.Event()
    invocation = ToolInvocation("no-window", "spawn_agent", {}, 0)
    task = ScheduledTool(
        invocation, ToolPurity.PURE_READ, lambda: (wedge.wait(T(30)), "never")[1],
        timeout_safe=False, request_cancel=lambda _k: None, cancel_grace=T(0.05),
        activity=ChildActivity(),
    )
    box: dict = {}
    runner = threading.Thread(
        target=lambda: box.setdefault(
            "outcomes", run_ordered([task], lifecycle_timeout=None, lifecycle_absolute=T(0.5))),
        daemon=True)
    runner.start()
    runner.join(T(12.0))
    frozen = runner.is_alive()
    wedge.set()
    assert not frozen, (
        "with lifecycle_timeout=None (the API DEFAULT) the absolute leak guard never fired — a "
        "non-terminating child holds the parent turn open indefinitely"
    )
    assert box["outcomes"][0].status in (
        ToolStatus.INDETERMINATE, ToolStatus.FAILED, ToolStatus.CANCELLED), box["outcomes"][0].status


@check
def queued_child_inactivity_starts_at_physical_admission():
    first_activity = ChildActivity()
    second_activity = ChildActivity()
    first_inv = ToolInvocation("first-child", "spawn_agent", {}, 0)
    second_inv = ToolInvocation("queued-child", "spawn_agent", {}, 1)
    second_started = []

    def first():
        until = time.monotonic() + T(0.12)
        while time.monotonic() < until:
            first_activity.touch()
            time.sleep(T(0.01))
        return ToolOutcome(first_inv, ToolStatus.SUCCEEDED, "first complete")

    def second():
        second_started.append(True)
        time.sleep(T(0.02))
        return ToolOutcome(second_inv, ToolStatus.SUCCEEDED, "queued child complete")

    outcomes = run_ordered([
        ScheduledTool(
            first_inv, ToolPurity.PURE_READ, first, timeout_safe=False,
            request_cancel=lambda _kind: None, activity=first_activity,
        ),
        ScheduledTool(
            second_inv, ToolPurity.PURE_READ, second, timeout_safe=False,
            request_cancel=lambda _kind: None, activity=second_activity,
        ),
    ], max_workers=1, lifecycle_timeout=T(0.05), lifecycle_absolute=T(1.0))
    assert second_started == [True], "queue wait was incorrectly charged as child inactivity"
    assert [outcome.status for outcome in outcomes] == [
        ToolStatus.SUCCEEDED, ToolStatus.SUCCEEDED,
    ]


@check
def parent_cancellation_outranks_child_liveness_cutoffs():
    parent_cancel = threading.Event()
    child_cancel = threading.Event()
    activity = ChildActivity()
    invocation = ToolInvocation("cancelled-child", "spawn_agent", {}, 0)
    entered = threading.Event()
    box = {}

    def child():
        entered.set()
        assert child_cancel.wait(T(1))
        return ToolOutcome(invocation, ToolStatus.CANCELLED, "child closed")

    runner = threading.Thread(target=lambda: box.setdefault("outcomes", run_ordered([
        ScheduledTool(
            invocation, ToolPurity.PURE_READ, child, timeout_safe=False,
            request_cancel=lambda _kind: child_cancel.set(), cancel_grace=T(0.2), activity=activity,
        ),
    ], lifecycle_timeout=T(0.5), lifecycle_absolute=T(1.0), should_cancel=parent_cancel.is_set)), daemon=True)
    runner.start()
    assert entered.wait(T(1))
    parent_cancel.set()
    runner.join(T(1))
    assert not runner.is_alive()
    assert box["outcomes"][0].status is ToolStatus.CANCELLED
    assert "parent turn cancellation" in box["outcomes"][0].text
    assert "inactivity" not in box["outcomes"][0].text


@check
def registered_tool_status_does_not_come_from_text():
    registry = ToolRegistry()
    registry.register(ToolEntry(
        "read_file",
        {"type": "function", "function": {"name": "read_file", "parameters": {}}},
        lambda _args: "Error: this is legitimate file content",
    ))
    outcome = registry.invoke(ToolInvocation("c1", "read_file", {}, 0))
    assert outcome.status is ToolStatus.SUCCEEDED
    assert not outcome.failing
    assert outcome.effects[0].kind == "tool_outcome"
    assert outcome.effects[0].payload["status"] == "succeeded"


@check
def invalid_explicit_status_never_fabricates_success():
    value = ToolText("provider supplied an unknown status", status="definitely-not-a-status")
    assert value.status is ToolStatus.INDETERMINATE
    registry = ToolRegistry()
    registry.register(ToolEntry(
        "opaque", {"type": "function", "function": {"name": "opaque", "parameters": {}}},
        lambda _args: value,
    ))
    outcome = registry.invoke(ToolInvocation("bad-status", "opaque", {}, 0))
    assert outcome.status is ToolStatus.INDETERMINATE


@check
def registry_enforces_live_availability_and_canonical_result_coercion():
    ran = []
    registry = ToolRegistry()
    schema = lambda name: {"type": "function", "function": {"name": name, "parameters": {}}}
    registry.register(ToolEntry(
        "offline", schema("offline"), lambda _args: ran.append("offline") or "bad",
        check=lambda: False,
    ))
    assert "offline" not in registry.names()
    unavailable = registry.invoke(ToolInvocation("offline", "offline", {}, 0))
    assert unavailable.status is ToolStatus.FAILED and ran == []

    registry.register(ToolEntry(
        "bytes", schema("bytes"), lambda _args: b"\xffpayload", purity=ToolPurity.PURE_READ,
    ))
    decoded = registry.invoke(ToolInvocation("bytes", "bytes", {}, 1))
    assert decoded.status is ToolStatus.SUCCEEDED and decoded.text == "\ufffdpayload"

    class BrokenText:
        def __str__(self):
            raise RuntimeError("cannot render result")

    registry.register(ToolEntry(
        "broken", schema("broken"), lambda _args: BrokenText(),
        source="plugin:test", purity=ToolPurity.UNKNOWN,
    ))
    broken = registry.invoke(ToolInvocation("broken", "broken", {}, 2))
    assert broken.status is ToolStatus.INDETERMINATE
    assert "cannot render result" in broken.text


@check
def extension_system_exit_is_contained_but_keyboard_interrupt_still_escapes():
    schema = lambda name: {"type": "function", "function": {"name": name, "parameters": {}}}
    registry = ToolRegistry()
    registry.register(ToolEntry(
        "exit_check", schema("exit_check"), lambda _args: "unexpected",
        check=lambda: (_ for _ in ()).throw(SystemExit(9)), source="plugin:test",
    ))
    registry.register(ToolEntry(
        "exit_access", schema("exit_access"), lambda _args: "unexpected",
        accesses=lambda _args: (_ for _ in ()).throw(SystemExit(8)), source="plugin:test",
    ))
    registry.register(ToolEntry(
        "exit_handler", schema("exit_handler"),
        lambda _args: (_ for _ in ()).throw(SystemExit(7)), source="plugin:test",
    ))
    assert "exit_check" not in registry.names()
    from sliceagent.access import AllAccess
    assert isinstance(registry.accesses("exit_access", {})[0], AllAccess)
    assert registry.run("exit_handler", {}).status is ToolStatus.INDETERMINATE

    registry.register(ToolEntry(
        "exit_effect", schema("exit_effect"), lambda _args: "ran",
        source="plugin:test", effect_factory=lambda *_args: (_ for _ in ()).throw(SystemExit(6)),
    ))
    effect_outcome = registry.invoke(ToolInvocation("exit-effect", "exit_effect", {}, 0))
    assert effect_outcome.status is ToolStatus.INDETERMINATE
    assert "effect construction failed" in effect_outcome.text

    registry.register(ToolEntry(
        "interrupt", schema("interrupt"),
        lambda _args: (_ for _ in ()).throw(KeyboardInterrupt()), source="plugin:test",
    ))
    try:
        registry.run("interrupt", {})
        assert False, "KeyboardInterrupt remains user-owned and must escape"
    except KeyboardInterrupt:
        pass


@check
def malformed_tool_schema_is_rejected_atomically():
    registry = ToolRegistry()
    generation = registry.generation
    malformed = (
        {"type": "function", "function": {"name": "bad", "parameters": "not-a-schema"}},
        {"type": "function", "function": {"name": "bad", "parameters": ""}},
        {"type": "function", "function": {"name": "bad", "parameters": {"required": ""}}},
        {"type": "function", "function": {"name": "bad", "parameters": {"required": 0}}},
    )
    for schema in malformed:
        try:
            registry.register(ToolEntry("bad", schema, lambda _args: "bad"))
            assert False, "a malformed schema must not enter the shared registry"
        except ValueError:
            pass
    assert not registry.has("bad") and registry.generation == generation


@check
def registry_invoke_separates_handler_args_from_raw_effect_provenance():
    handled, constructed = [], []

    def handler(args):
        handled.append(dict(args))
        return "ok"

    def effects(invocation, status, text):
        constructed.append((dict(invocation.args), status, text))
        return (ToolEffect("custom-effect", "custom", {"path": invocation.args["path"]}),)

    registry = ToolRegistry()
    registry.register(ToolEntry(
        "edit_file", {"type": "function", "function": {
            "name": "edit_file", "parameters": {"required": ["path"]},
        }}, handler, effect_factory=effects,
    ))
    invocation = ToolInvocation(
        "edit-1", "edit_file", {"path": "a.py", "note": "raw provenance"}, 0,
    )
    outcome = registry.invoke(
        invocation, call_args={"path": "a.py"}, default_effect_id="unused-default",
    )
    assert handled == [{"path": "a.py"}]
    assert constructed == [(
        {"path": "a.py", "note": "raw provenance"}, ToolStatus.SUCCEEDED, "ok",
    )]
    assert outcome.effects == (ToolEffect("custom-effect", "custom", {"path": "a.py"}),)

    handled.clear()
    constructed.clear()

    class Host:
        def __init__(self):
            self.registry = registry

        def accesses(self, name, args):
            return registry.accesses(name, args)

        def run(self, name, args):
            return registry.run(name, args)

        def preflight_run(self, name, args):
            return registry.admit(name, args)

        def run_preflighted(self, name, args, admission):
            return registry.run_admitted(admission, args)

    _, rows = run_tool_batch(
        [_tc("edit_file", {"path": "a.py", "note": "raw provenance"}, "edit-2")],
        Host(), lambda _event: None, Hooks(), step=2, turn_id="turn-A",
    )
    assert handled == [{"path": "a.py"}], "handler must receive note-stripped call args"
    assert constructed[0][0] == {"path": "a.py", "note": "raw provenance"}
    assert rows[0]["outcome"].effects[0].id == "custom-effect"


@check
def registry_and_production_share_effect_factory_failure_semantics():
    ran = []

    def broken_effects(_invocation, _status, _text):
        raise RuntimeError("cannot construct effects")

    registry = ToolRegistry()
    def schema(name):
        return {"type": "function", "function": {"name": name, "parameters": {}}}

    registry.register(ToolEntry(
        "first_write", schema("first_write"),
        lambda _args: ran.append("first_write") or "wrote", effect_factory=broken_effects,
    ))
    registry.register(ToolEntry(
        "second_write", schema("second_write"),
        lambda _args: ran.append("second_write") or "wrote",
    ))

    direct = registry.invoke(
        ToolInvocation("direct", "first_write", {}, 0), default_effect_id="direct-default",
    )
    assert direct.status is ToolStatus.INDETERMINATE
    assert direct.effects[0].id == "direct-default"
    assert direct.effects[0].payload["status"] == "indeterminate"

    ran.clear()

    class Host:
        def __init__(self):
            self.registry = registry

        def accesses(self, name, args):
            return registry.accesses(name, args)

        def run(self, name, args):
            return registry.run(name, args)

    _, results = run_tool_batch(
        [_tc("first_write", {}, "one"), _tc("second_write", {}, "two")],
        Host(), lambda _event: None, Hooks(), step=3, turn_id="turn-A",
    )
    assert [row["status"] for row in results] == ["indeterminate", "cancelled"]
    assert ran == ["first_write"]
    effect = results[0]["outcome"].effects[0]
    assert effect.id == "turn-A:3:0:one:0"
    assert effect.payload == {"name": "first_write", "status": "indeterminate"}


@check
def preflight_stop_uses_canonical_effect_without_running_handler():
    from sliceagent.hooks import ToolPreflight

    ran = []
    registry = ToolRegistry()
    registry.register(ToolEntry(
        "edit_file", {"type": "function", "function": {
            "name": "edit_file", "parameters": {},
        }}, lambda _args: ran.append("handler") or "edited",
    ))

    class Host:
        def __init__(self):
            self.registry = registry

        def accesses(self, name, args):
            return registry.accesses(name, args)

        def run(self, name, args):
            return registry.run(name, args)

    class LifecycleStop(Hooks):
        def preflight_tool(self, _name, _args):
            return ToolPreflight(True, "cancelled for test", kind="lifecycle")

    _, rows = run_tool_batch(
        [_tc("edit_file", {"path": "a.py", "note": "keep raw"}, "blocked")],
        Host(), lambda _event: None, LifecycleStop(), step=4, turn_id="turn-P",
    )
    assert ran == []
    outcome = rows[0]["outcome"]
    assert outcome.status is ToolStatus.CANCELLED
    assert rows[0]["rejection_kind"] == "lifecycle"
    assert rows[0]["output"] == "Not run: cancelled for test"
    assert "policy" not in rows[0]["output"].casefold()
    assert outcome.effects[0] == ToolEffect(
        "turn-P:4:0:blocked:0", "tool_outcome", {"name": "edit_file", "status": "cancelled"},
    )


@check
def preflight_stop_never_invokes_execution_effect_factory():
    from sliceagent.hooks import ToolPreflight

    ran = []
    registry = ToolRegistry()

    def effects_that_must_not_run(_invocation, _status, _text):
        raise RuntimeError("execution-only effect factory was called")

    def schema(name):
        return {"type": "function", "function": {"name": name, "parameters": {}}}

    registry.register(ToolEntry(
        "blocked", schema("blocked"), lambda _args: ran.append("blocked") or "bad",
        purity=ToolPurity.EFFECTFUL, effect_factory=effects_that_must_not_run,
    ))
    registry.register(ToolEntry(
        "later", schema("later"), lambda _args: ran.append("later") or "ok",
        purity=ToolPurity.EFFECTFUL,
    ))

    class Host:
        def __init__(self):
            self.registry = registry

        def accesses(self, name, args):
            return registry.accesses(name, args)

        def run(self, name, args):
            return registry.run(name, args)

    class StopFirst(Hooks):
        def preflight_tool(self, name, _args):
            return ToolPreflight(name == "blocked", "cancelled before execution", kind="lifecycle")

    _, rows = run_tool_batch([
        _tc("blocked", {}, "blocked"), _tc("later", {}, "later"),
    ], Host(), lambda _event: None, StopFirst())
    assert [row["status"] for row in rows] == ["cancelled", "succeeded"]
    assert ran == ["later"]


@check
def registry_validation_failures_never_claim_physical_handler_start():
    from sliceagent.events import ToolExecutionStarted, ToolStarted

    ran, events = [], []
    registry = ToolRegistry()
    schema = lambda name, required=(): {"type": "function", "function": {
        "name": name,
        "parameters": {"type": "object", "required": list(required)},
    }}
    registry.register(ToolEntry(
        "offline", schema("offline"), lambda _args: ran.append("offline") or "bad",
        check=lambda: False,
    ))
    registry.register(ToolEntry(
        "needs_path", schema("needs_path", ("path",)),
        lambda _args: ran.append("needs_path") or "bad",
    ))

    class Host:
        def __init__(self):
            self.registry = registry

        def accesses(self, name, args):
            return registry.accesses(name, args)

        def run(self, name, args):
            return registry.run(name, args)

        def preflight_run(self, name, args):
            return registry.admit(name, args)

        def run_preflighted(self, name, args, admission):
            return registry.run_admitted(admission, args)

    _, rows = run_tool_batch([
        _tc("offline", {}, "offline"),
        _tc("needs_path", {}, "missing"),
        _tc("not_registered", {}, "unknown"),
    ], Host(), events.append, Hooks())
    assert [row["status"] for row in rows] == ["failed", "failed", "failed"]
    assert ran == []
    assert not any(isinstance(event, (ToolExecutionStarted, ToolStarted)) for event in events)


@check
def scheduled_registry_admission_is_one_shot_across_the_start_boundary():
    from sliceagent.events import ToolStarted

    checks, ran, events = [], [], []
    registry = ToolRegistry()

    def volatile_check():
        checks.append(len(checks) + 1)
        return len(checks) == 1

    registry.register(ToolEntry(
        "volatile", {"type": "function", "function": {
            "name": "volatile", "parameters": {},
        }}, lambda _args: ran.append("handler") or "ok", check=volatile_check,
    ))

    class Host:
        def accesses(self, name, args):
            return registry.accesses(name, args)

        def preflight_run(self, name, args):
            return registry.admit(name, args)

        def run_preflighted(self, name, args, admission):
            return registry.run_admitted(admission, args)

        def run(self, name, args):
            return registry.run(name, args)

    _, rows = run_tool_batch(
        [_tc("volatile", {}, "volatile")], Host(), events.append, Hooks(),
    )
    assert checks == [1], "availability must not be rechecked after ToolStarted"
    assert ran == ["handler"] and rows[0]["status"] == "succeeded"
    assert any(isinstance(event, ToolStarted) for event in events)


@check
def direct_registry_preflight_failure_never_invokes_execution_effect_factory():
    effects, ran = [], []
    registry = ToolRegistry()

    def factory(*_args):
        effects.append("factory")
        return ()

    registry.register(ToolEntry(
        "offline", {"type": "function", "function": {
            "name": "offline", "parameters": {},
        }}, lambda _args: ran.append("handler") or "bad", check=lambda: False,
        effect_factory=factory,
    ))
    outcome = registry.invoke(ToolInvocation("offline", "offline", {}, 0))
    assert outcome.status is ToolStatus.FAILED
    assert ran == [] and effects == []
    assert outcome.effects[0].kind == "tool_outcome"


@check
def incomplete_host_preflight_protocol_never_crosses_the_start_boundary():
    from sliceagent.events import ToolExecutionStarted, ToolStarted

    preflighted, ran, events = [], [], []

    class Host:
        def accesses(self, _name, _args):
            return []

        def preflight_run(self, name, _args):
            preflighted.append(name)
            return object(), None

        def run(self, name, _args):
            ran.append(name)
            return "bad"

    _, rows = run_tool_batch(
        [_tc("partial", {}, "partial")], Host(), events.append, Hooks(),
    )
    assert preflighted == [], "an unpaired preflight method must not claim one-shot admission"
    assert ran == [] and rows[0]["status"] == "failed"
    assert "incomplete one-shot preflight protocol" in rows[0]["output"]
    assert not any(isinstance(event, (ToolExecutionStarted, ToolStarted)) for event in events)


@check
def same_purity_registry_replacement_cannot_inherit_stale_dedup_or_effect_metadata():
    from sliceagent.events import ToolStarted

    old_effects, new_effects, ran = [], [], []
    events = []
    registry = ToolRegistry()

    def schema(name):
        return {"type": "function", "function": {"name": name, "parameters": {}}}

    def old_factory(*_args):
        old_effects.append("old")
        return (ToolEffect("old", "old", {}),)

    def new_factory(*_args):
        new_effects.append("new")
        return (ToolEffect("new", "new", {}),)

    registry.register(ToolEntry(
        "target", schema("target"), lambda _args: ran.append("old-handler") or "old",
        purity=ToolPurity.PURE_READ, deduplicable=True, effect_factory=old_factory,
    ))

    def replace(_args):
        registry.register(ToolEntry(
            "target", schema("target"), lambda _args: ran.append("new-handler") or "new",
            purity=ToolPurity.PURE_READ, deduplicable=False, effect_factory=new_factory,
        ), override=True)
        return "replaced"

    registry.register(ToolEntry(
        "replace", schema("replace"), replace, purity=ToolPurity.EFFECTFUL,
    ))

    class Host:
        def __init__(self):
            self.registry = registry

        def accesses(self, name, args):
            return registry.accesses(name, args)

        def preflight_run(self, name, args):
            return registry.admit(name, args)

        def run_preflighted(self, name, args, admission):
            return registry.run_admitted(admission, args)

        def run(self, name, args):
            return registry.run(name, args)

    _, rows = run_tool_batch([
        _tc("replace", {}, "replace"),
        _tc("target", {}, "target-1"), _tc("target", {}, "target-2"),
    ], Host(), events.append, Hooks())
    assert ran == [] and old_effects == [] and new_effects == []
    assert [row["status"] for row in rows] == ["succeeded", "failed", "failed"]
    assert "registration changed before execution" in rows[1]["output"]
    started = [event.invocation.id for event in events if isinstance(event, ToolStarted)]
    assert started == ["replace"], "neither the stale source nor its collapsed duplicate may start"


@check
def registry_replacement_cannot_run_under_stale_scheduler_purity():
    from sliceagent.access import AllAccess, ReadAllAccess
    from sliceagent.events import ToolStarted

    ran, events = [], []
    registry = ToolRegistry()

    def schema(name):
        return {"type": "function", "function": {"name": name, "parameters": {}}}

    registry.register(ToolEntry(
        "target", schema("target"), lambda _args: ran.append("old-read") or "old",
        accesses=lambda _args: [ReadAllAccess()], purity=ToolPurity.PURE_READ,
    ))

    def replace(_args):
        registry.register(ToolEntry(
            "target", schema("target"), lambda _args: ran.append("new-write") or "new",
            accesses=lambda _args: [AllAccess()], purity=ToolPurity.EFFECTFUL,
        ), override=True)
        return "replaced"

    registry.register(ToolEntry(
        "replace", schema("replace"), replace,
        accesses=lambda _args: [AllAccess()], purity=ToolPurity.EFFECTFUL,
    ))

    class Host:
        def __init__(self):
            self.registry = registry

        def accesses(self, name, args):
            return registry.accesses(name, args)

        def preflight_run(self, name, args):
            return registry.admit(name, args)

        def run_preflighted(self, name, args, admission):
            return registry.run_admitted(admission, args)

        def run(self, name, args):
            return registry.run(name, args)

    _, rows = run_tool_batch([
        _tc("replace", {}, "replace"), _tc("target", {}, "target"),
    ], Host(), events.append, Hooks())
    assert ran == [], "the replacement must not run with the old read-wave timeout/concurrency contract"
    assert rows[1]["status"] == "failed"
    assert "registration changed before execution" in rows[1]["output"]
    started = [event.invocation.id for event in events if isinstance(event, ToolStarted)]
    assert started == ["replace"]


@check
def subagent_wrapper_preserves_incomplete_inner_preflight_rejection():
    from sliceagent.events import ToolExecutionStarted, ToolStarted
    from sliceagent.scoped_spawn import ScopedSpawnHost

    preflighted, ran, events = [], [], []

    class Inner:
        def accesses(self, _name, _args):
            return []

        def preflight_run(self, name, _args):
            preflighted.append(name)
            return object(), None

        def run(self, name, _args):
            ran.append(name)
            return "bad"

    host = ScopedSpawnHost(Inner(), llm=None, retriever=None, memory=None)
    _, rows = run_tool_batch(
        [_tc("partial", {}, "partial")], host, events.append, Hooks(),
    )
    assert preflighted == [] and ran == []
    assert rows[0]["status"] == "failed"
    assert "incomplete one-shot preflight protocol" in rows[0]["output"]
    assert not any(isinstance(event, (ToolExecutionStarted, ToolStarted)) for event in events)


@check
def dynamic_host_exception_also_flows_through_canonical_default_effect():
    class Host:
        def accesses(self, _name, _args):
            return []  # no explicit pure-read contract => UNKNOWN and potentially effectful

        def run(self, _name, _args):
            raise RuntimeError("boundary failed")

    _, rows = run_tool_batch(
        [_tc("opaque_extension", {}, "opaque")], Host(), lambda _event: None, Hooks(),
        step=5, turn_id="turn-D",
    )
    outcome = rows[0]["outcome"]
    assert outcome.status is ToolStatus.INDETERMINATE
    assert outcome.effects[0] == ToolEffect(
        "turn-D:5:0:opaque:0", "tool_outcome",
        {"name": "opaque_extension", "status": "indeterminate"},
    )


@check
def provider_order_prevents_read_from_overtaking_write():
    state = {"value": "old"}

    class Host:
        def accesses(self, _name, _args):
            return []

        def run(self, name, _args):
            if name == "read_file":
                time.sleep(T(0.03))
                return state["value"]
            state["value"] = "new"
            return "written"

    calls = [
        _tc("read_file", {"path": "x"}, "r1"),
        _tc("edit_file", {"path": "x", "content": "new"}, "w"),
        _tc("read_file", {"path": "x"}, "r2"),
    ]
    _, results = run_tool_batch(calls, Host(), lambda _event: None, Hooks())
    assert [r["output"] for r in results] == ["old", "written", "new"]


@check
def consecutive_pure_reads_overlap():
    rendezvous = threading.Barrier(2)

    class Host:
        def accesses(self, _name, _args):
            return []

        def run(self, _name, args):
            rendezvous.wait(timeout=1)
            return args["path"]

    calls = [_tc("read_file", {"path": "a"}, "a"), _tc("read_file", {"path": "b"}, "b")]
    _, results = run_tool_batch(calls, Host(), lambda _event: None, Hooks())
    assert [r["status"] for r in results] == ["succeeded", "succeeded"]


@check
def unkillable_effectful_timeout_waits_before_later_barrier():
    prior = os.environ.get("AGENT_TOOL_TIMEOUT")
    os.environ["AGENT_TOOL_TIMEOUT"] = str(T(0.03))
    ran = []

    class Host:
        def accesses(self, _name, _args):
            return []

        def run(self, name, _args):
            ran.append(name)
            if name == "unknown_mutator":
                time.sleep(T(0.12))
            return "ok"

    calls = [_tc("unknown_mutator", {}, "slow"), _tc("edit_file", {"path": "x"}, "later")]
    try:
        _, results = run_tool_batch(calls, Host(), lambda _event: None, Hooks())
        assert [r["status"] for r in results] == ["succeeded", "succeeded"]
        assert ran == ["unknown_mutator", "edit_file"]
    finally:
        if prior is None:
            os.environ.pop("AGENT_TOOL_TIMEOUT", None)
        else:
            os.environ["AGENT_TOOL_TIMEOUT"] = prior


@check
def local_command_timeout_is_adopted_with_progress_preserved():
    from sliceagent.tools import LocalToolHost

    with tempfile.TemporaryDirectory(prefix="command-timeout-") as root:
        target = os.path.join(root, "late.txt")
        host = LocalToolHost(root=root, timeout=1)
        command = f"(sleep 1.4; echo late > {shlex.quote(target)}) & sleep 10"
        outcome = host.run("run_command", {"command": command, "timeout": 1})
        # The deadline now ADOPTS the live process into the background registry (Kimi Code's
        # autoBackgroundOnTimeout) instead of reaping it: not ✗, not a verdict, and the descendant
        # finish line is PRESERVED — the reap path could never produce it.
        assert outcome.status is ToolStatus.SUCCEEDED
        assert "was NOT killed" in str(outcome)
        time.sleep(T(1.2))
        if os.name != "nt":
            assert os.path.exists(target), (
                "adoption must preserve late work; only a reap would make this file absent")
        import re as _re
        handle = _re.search(r"background as (p\d+)", str(outcome)).group(1)
        host.run("proc_kill", {"handle": handle})


@check
def execute_code_inner_timeout_is_failed_and_reaps_background_tree():
    from sliceagent.tools import LocalToolHost

    with tempfile.TemporaryDirectory(prefix="execute-inner-timeout-") as root:
        target = os.path.join(root, "late.txt")
        host = LocalToolHost(root=root, timeout=8)
        command = f"(sleep 1.4; echo late > {shlex.quote(target)}) & sleep 10"
        outcome = host.run("execute_code", {"code": f"run({command!r}, timeout=1)"})
        assert outcome.status is ToolStatus.FAILED, outcome
        assert "re-read before re-running" in str(outcome)
        time.sleep(T(0.6))
        if os.name != "nt":   # Windows taskkill /T is best-effort on a detached `&` subshell — see above
            assert not os.path.exists(target), "the nested run() descendant mutated after timeout return"


@check
def read_only_child_is_parallelizable_but_not_abandoned_by_generic_timeout():
    from sliceagent.access import ReadAllAccess

    prior = os.environ.get("AGENT_TOOL_TIMEOUT")
    os.environ["AGENT_TOOL_TIMEOUT"] = str(T(0.03))

    class Host:
        def accesses(self, _name, _args):
            return [ReadAllAccess()]

        def run(self, _name, _args):
            time.sleep(T(0.1))
            return "sealed child"

    started = time.monotonic()
    try:
        _, results = run_tool_batch(
            [_tc("spawn_agent", {"agent": "explorer", "task": "inspect"}, "child")],
            Host(), lambda _event: None, Hooks(),
        )
    finally:
        if prior is None:
            os.environ.pop("AGENT_TOOL_TIMEOUT", None)
        else:
            os.environ["AGENT_TOOL_TIMEOUT"] = prior
    assert time.monotonic() - started >= T(0.09)
    assert results[0]["status"] == "succeeded"


@check
def pure_read_timeout_returns_failure_feedback_without_reconciliation():
    prior = os.environ.get("AGENT_TOOL_TIMEOUT")
    os.environ["AGENT_TOOL_TIMEOUT"] = str(T(0.03))

    class LLM:
        def __init__(self):
            self.calls = 0

        def complete(self, _messages, _schemas):
            self.calls += 1
            if self.calls > 1:
                return NS(content="The read timed out.", tool_calls=[], finish_reason="stop", usage={})
            return NS(content="", tool_calls=[_tc("read_file", {"path": "slow"}, "slow")],
                      finish_reason="tool_calls", usage={})

    class Host:
        def schemas(self):
            return []

        def accesses(self, _name, _args):
            return []

        def run(self, _name, _args):
            time.sleep(T(0.06))
            return "late"

    llm, events = LLM(), []
    try:
        result = run_turn(
            build_slice=lambda: [{"role": "user", "content": "go"}],
            llm=llm, tools=Host(), dispatch=events.append, hooks=Hooks(), max_steps=4,
        )
        assert result.stop_reason == "end_turn"
        assert llm.calls == 2
        tool_results = [e for e in events if isinstance(e, ToolResult)]
        assert tool_results and tool_results[0].status == "failed"
        assert not any(isinstance(e, TurnInterrupted) for e in events)
        assert any(isinstance(e, TurnEnd) for e in events)
    finally:
        if prior is None:
            os.environ.pop("AGENT_TOOL_TIMEOUT", None)
        else:
            os.environ["AGENT_TOOL_TIMEOUT"] = prior


@check
def keyboard_interrupt_after_tool_start_records_exact_reconciliation_target():
    from sliceagent.pfc import Slice, slice_sink

    state = Slice(); state.reset("edit")

    class LLM:
        def complete(self, _messages, _schemas):
            return NS(
                content="", tool_calls=[_tc("edit_file", {"path": "critical.py"}, "edit-1")],
                finish_reason="tool_calls", usage={},
            )

    class Host:
        def schemas(self):
            return []

        def accesses(self, _name, _args):
            return []

        def run(self, _name, _args):
            raise KeyboardInterrupt

    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=LLM(), tools=Host(), dispatch=slice_sink(state), hooks=Hooks(), max_steps=2,
    )
    assert result.stop_reason == "indeterminate"
    assert state.reconciliation_targets == ["path:critical.py"]
    assert "edit-1" in state.reconciliation_required


@check
def execution_uncertainty_is_advisory_on_the_next_turn():
    from sliceagent.pfc import Slice, slice_sink

    state = Slice(); state.reset("repair")
    state.reconciliation_required = "late command may still write"
    state.reconciliation_targets = ["path:a.py"]
    ran = []

    class LLM:
        def __init__(self):
            self.calls = 0

        def complete(self, _messages, _schemas):
            self.calls += 1
            usage = {"prompt_tokens": 1, "completion_tokens": 1}
            if self.calls == 1:
                return NS(content="", tool_calls=[_tc("edit_file", {"path": "a.py"}, "ordinary-edit")],
                          finish_reason="tool_calls", usage=usage)
            return NS(content="done", tool_calls=[], finish_reason="stop", usage=usage)

    class Host:
        def schemas(self):
            return []

        def accesses(self, _name, _args):
            return []

        def run(self, name, _args):
            ran.append(name)
            return ToolText("ok")

    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "continue safely"}],
        llm=LLM(), tools=Host(), dispatch=slice_sink(state),
        hooks=Hooks(), max_steps=4,
    )
    assert result.stop_reason == "end_turn"
    assert ran == ["edit_file"], "historical uncertainty must not become an execution blocker"
    assert state.reconciliation_required == "late command may still write", \
        "the receipt remains truthful until explicitly resolved"


@check
def incidental_arguments_cannot_narrow_an_opaque_operation():
    args = {
        "command": "curl -X POST example.test/deploy",
        "path": "README.md", "handle": "p1", "session": "main",
    }
    assert reconciliation_targets("run_command", args) == (
        "workspace:*", "opaque:run_command",
    )


@check
def unknown_mcp_uncertainty_retains_local_opaque_and_external_boundaries():
    assert reconciliation_targets("mcp__deploy__publish", {"path": "README.md"}) == (
        "workspace:*", "opaque:mcp__deploy__publish", "external:mcp__deploy__publish",
    )


@check
def local_ctrl_c_reaps_the_started_command_group():
    from sliceagent.sandbox import LocalSandbox

    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory(prefix="command-interrupt-") as root:
        target = os.path.join(root, "late.txt")
        command = f"(sleep 0.8; echo late > {shlex.quote(target)}) & sleep 10"
        timer = threading.Timer(0.15, lambda: os.kill(os.getpid(), signal.SIGINT))
        timer.start()
        try:
            try:
                LocalSandbox().run(command, cwd=root, timeout=20)
                assert False, "the injected SIGINT must reach the blocking command"
            except KeyboardInterrupt:
                pass
        finally:
            timer.cancel()
        time.sleep(T(1.0))
        assert not os.path.exists(target), "an interrupted command descendant mutated after the turn returned"


@check
def missing_user_answer_is_a_typed_cancellation():
    from sliceagent.tools import LocalToolHost
    host = LocalToolHost(tempfile.mkdtemp(prefix="ask-user-cancel-"))
    host.on_ask_user = lambda _question, _options: "(no answer)"
    output = host.run("ask_user", {"question": "Did it settle?"})
    assert isinstance(output, ToolText) and output.status is ToolStatus.CANCELLED


@check
def read_settling_during_grace_preserves_later_barrier():
    prior = os.environ.get("AGENT_TOOL_TIMEOUT")
    os.environ["AGENT_TOOL_TIMEOUT"] = str(T(0.03))
    ran, events = [], []

    class Host:
        def accesses(self, _name, _args):
            return []

        def run(self, name, _args):
            ran.append(f"{name}:start")
            if name == "read_file":
                time.sleep(T(0.06))
                ran.append("read_file:end")
            return "ok"

    try:
        _, results = run_tool_batch([
            _tc("read_file", {"path": "slow"}, "slow"),
            _tc("edit_file", {"path": "later", "content": "x"}, "later"),
        ], Host(), events.append, Hooks())
        assert [result["status"] for result in results] == ["failed", "succeeded"]
        assert ran == ["read_file:start", "read_file:end", "edit_file:start"], \
            "the timeout is a normal failure, but the read must remain an ordering barrier until it exits"
        from sliceagent.events import ToolResult
        logical = [event for event in events if isinstance(event, ToolResult)]
        assert [event.invocation_id for event in logical] == ["slow", "later"]
    finally:
        if prior is None:
            os.environ.pop("AGENT_TOOL_TIMEOUT", None)
        else:
            os.environ["AGENT_TOOL_TIMEOUT"] = prior


@check
def hung_read_returns_indeterminate_cancels_tail_and_releases_fixture():
    release = threading.Event()
    finished = threading.Event()
    ran_effect = []
    read_inv = ToolInvocation("hung", "read_file", {"path": "fifo"}, 0)
    edit_inv = ToolInvocation("edit", "edit_file", {"path": "later"}, 1)

    def hung_read():
        try:
            release.wait()
            return ToolOutcome(read_inv, ToolStatus.SUCCEEDED, "late")
        finally:
            finished.set()

    def edit():
        ran_effect.append(True)
        return ToolOutcome(edit_inv, ToolStatus.SUCCEEDED, "edited")

    started = time.monotonic()
    try:
        outcomes = run_ordered([
            ScheduledTool(read_inv, ToolPurity.PURE_READ, hung_read),
            ScheduledTool(edit_inv, ToolPurity.EFFECTFUL, edit),
        ], timeout=T(0.03))
        elapsed = time.monotonic() - started
        assert [outcome.status for outcome in outcomes] == [
            ToolStatus.INDETERMINATE, ToolStatus.CANCELLED,
        ]
        assert not ran_effect, "a later mutation must not overtake a still-running reader"
        assert elapsed < T(0.35), "the scheduler must return after deadline + bounded grace"
    finally:
        release.set()
        assert finished.wait(T(1)), "the daemon read fixture must settle after release"


@check
def hung_read_polls_turn_cancellation_during_long_deadline():
    release = threading.Event()
    finished = threading.Event()
    cancel = threading.Event()
    inv = ToolInvocation("cancel-read", "read_file", {"path": "fifo"}, 0)

    def hung_read():
        try:
            release.wait()
            return ToolOutcome(inv, ToolStatus.SUCCEEDED, "late")
        finally:
            finished.set()

    timer = threading.Timer(0.04, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        outcomes = run_ordered(
            [ScheduledTool(inv, ToolPurity.PURE_READ, hung_read)],
            timeout=5,
            should_cancel=cancel.is_set,
        )
        assert outcomes[0].status is ToolStatus.INDETERMINATE
        assert time.monotonic() - started < T(0.5), "cancellation must be polled inside the read wave"
    finally:
        timer.cancel()
        timer.join(timeout=1)
        release.set()
        assert finished.wait(T(1)), "the cancelled daemon read fixture must settle after release"


@check
def no_timeout_parallel_reads_honor_cancellation_without_joining_workers():
    release = threading.Event()
    cancel = threading.Event()
    finished = threading.Event()
    lock = threading.Lock()
    finished_count = 0

    def task(index):
        invocation = ToolInvocation(f"no-timeout-{index}", "read_file", {"path": str(index)}, index)

        def read():
            nonlocal finished_count
            try:
                release.wait()
                return ToolOutcome(invocation, ToolStatus.SUCCEEDED, "late")
            finally:
                with lock:
                    finished_count += 1
                    if finished_count == 2:
                        finished.set()

        return ScheduledTool(invocation, ToolPurity.PURE_READ, read)

    timer = threading.Timer(0.04, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        outcomes = run_ordered([task(0), task(1)], should_cancel=cancel.is_set)
        assert all(outcome.status is ToolStatus.INDETERMINATE for outcome in outcomes), outcomes
        assert time.monotonic() - started < T(0.5), "cancellation must not join no-timeout read workers"
    finally:
        timer.cancel()
        timer.join(timeout=1)
        release.set()
        assert finished.wait(T(1)), "both abandoned read fixtures must eventually release"


@check
def sigint_does_not_freeze_on_no_timeout_parallel_reads():
    code = textwrap.dedent("""
        import os
        import threading
        from sliceagent.execution import ToolInvocation, ToolOutcome, ToolPurity, ToolStatus
        from sliceagent.scheduler import ScheduledTool, run_ordered

        gate = threading.Event()
        ready_lock = threading.Lock()
        ready_path = __import__("sys").argv[1]
        self_interrupt = len(__import__("sys").argv) > 2 and __import__("sys").argv[2] == "self"
        def task(index):
            invocation = ToolInvocation(str(index), "read_file", {"path": str(index)}, index)
            def read():
                with ready_lock:
                    with open(ready_path, "a", encoding="utf-8") as ready:
                        ready.write(f"{index}\\n")
                gate.wait()
                return ToolOutcome(invocation, ToolStatus.SUCCEEDED, "late")
            return ScheduledTool(invocation, ToolPurity.PURE_READ, read)
        if self_interrupt:
            def interrupt_when_ready():
                import _thread
                import time
                while True:
                    try:
                        if len(open(ready_path, encoding="utf-8").read().splitlines()) == 2:
                            _thread.interrupt_main()
                            return
                    except OSError:
                        pass
                    time.sleep(T(0.01))
            threading.Thread(target=interrupt_when_ready, daemon=True).start()
        try:
            run_ordered([task(0), task(1)])
        except KeyboardInterrupt:
            os._exit(42)
    """)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "src"))
    with tempfile.TemporaryDirectory() as directory:
        ready_path = os.path.join(directory, "ready")
        process = subprocess.Popen(
            [sys.executable, "-c", code, ready_path, "self" if os.name == "nt" else "external"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            deadline = time.monotonic() + T(2)
            while time.monotonic() < deadline:
                try:
                    if len(open(ready_path, encoding="utf-8").read().splitlines()) == 2:
                        break
                except OSError:
                    pass
                time.sleep(T(0.02))
            else:
                output, _ = process.communicate(timeout=1)
                raise AssertionError(f"parallel read workers never became ready: {output!r}")
            # Windows' Popen.send_signal does not accept SIGINT (and CTRL_BREAK requires a real console plus
            # process-group wiring that CI deliberately lacks).  The child uses Python's cross-platform
            # interrupt_main seam there, exercising the same KeyboardInterrupt escape from the scheduler.
            if os.name != "nt":
                process.send_signal(signal.SIGINT)
            output, _ = process.communicate(timeout=1)
            assert process.returncode == 42, (process.returncode, output)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate(timeout=2)
            raise AssertionError(f"SIGINT remained stuck joining read workers: {output!r}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


@check
def completed_wave_is_retried_if_interrupt_crosses_scheduler_publication():
    invocation = ToolInvocation("completed", "read_file", {"path": "done"}, 0)
    deliveries = []

    def deliver(outcomes):
        deliveries.append(tuple((out.invocation.id, out.status) for out in outcomes))
        if len(deliveries) == 1:
            raise KeyboardInterrupt

    try:
        run_ordered([
            ScheduledTool(
                invocation, ToolPurity.PURE_READ,
                lambda: ToolOutcome(invocation, ToolStatus.SUCCEEDED, "sealed"),
            ),
        ], on_outcomes=deliver)
        assert False, "the user interrupt must remain observable after recovery delivery"
    except KeyboardInterrupt:
        pass

    assert deliveries == [
        (("completed", ToolStatus.SUCCEEDED),),
        (("completed", ToolStatus.SUCCEEDED),),
    ], "the materialized physical outcome must be handed off before SIGINT escapes"


@check
def interrupt_on_first_terminal_edge_preserves_every_completed_sibling():
    from sliceagent.events import ToolSettled

    class Host:
        def accesses(self, _name, _args):
            return []

        def run(self, _name, args):
            return f"sealed:{args['path']}"

    for interrupted_type in (ToolSettled, ToolResult):
        events = []
        interrupted = [False]

        def dispatch(event):
            if isinstance(event, interrupted_type) and not interrupted[0]:
                interrupted[0] = True
                # Raise before accepting the edge. Recovery may then retry it exactly once; a dispatcher that
                # accepted and raised would still be safe because required sinks key lifecycle rows by ID.
                raise KeyboardInterrupt
            events.append(event)

        try:
            run_tool_batch([
                _tc("read_file", {"path": "a"}, "child-a"),
                _tc("read_file", {"path": "b"}, "child-b"),
            ], Host(), dispatch, Hooks())
            assert False, "the recovered batch must still propagate the user's interrupt"
        except KeyboardInterrupt:
            pass

        settled = [event for event in events if isinstance(event, ToolSettled)]
        results = [event for event in events if isinstance(event, ToolResult)]
        assert [event.outcome.invocation.id for event in settled] == ["child-a", "child-b"]
        assert [event.invocation_id for event in results] == ["child-a", "child-b"]
        assert all(event.status == "succeeded" for event in results), \
            "finished siblings must never be re-labelled indeterminate"


@check
def interrupted_rejection_settlement_and_result_edges_are_completed_in_order():
    from sliceagent.events import ToolRejected, ToolSettled
    from sliceagent.hooks import ToolPreflight

    class Host:
        def accesses(self, _name, _args):
            return []

        def run(self, _name, _args):
            raise AssertionError("a preflight-stopped handler must not run")

    class Stop(Hooks):
        def preflight_tool(self, _name, _args):
            return ToolPreflight(True, "fixture stop", kind="lifecycle")

    for interrupted_type in (ToolRejected, ToolSettled, ToolResult):
        events = []
        interrupted = [False]

        def dispatch(event):
            if isinstance(event, interrupted_type) and not interrupted[0]:
                interrupted[0] = True
                raise KeyboardInterrupt
            events.append(event)

        try:
            run_tool_batch([_tc("opaque", {}, "stopped")], Host(), dispatch, Stop())
            assert False, "the recovered rejection must still propagate the user's interrupt"
        except KeyboardInterrupt:
            pass

        terminal = [event for event in events if isinstance(
            event, (ToolRejected, ToolSettled, ToolResult),
        )]
        assert [type(event) for event in terminal] == [ToolRejected, ToolSettled, ToolResult]
        assert terminal[-1].status == "cancelled"


@check
def interrupt_inside_start_publication_closes_partial_start_without_running_handler():
    from sliceagent.events import ToolExecutionStarted, ToolSettled, ToolStarted

    events = []
    handler_ran = []
    interrupted = [False]

    class Host:
        def accesses(self, _name, _args):
            return []

        def run(self, _name, _args):
            handler_ran.append(True)
            return "unexpected"

    def dispatch(event):
        events.append(event)
        if isinstance(event, ToolExecutionStarted) and not interrupted[0]:
            interrupted[0] = True
            # Model a required journal that durably accepted the start row just before SIGINT.
            raise KeyboardInterrupt

    try:
        run_tool_batch([_tc("opaque", {}, "partial-start")], Host(), dispatch, Hooks())
        assert False, "the recovered partial start must still propagate the user's interrupt"
    except KeyboardInterrupt:
        pass

    assert handler_ran == []
    assert not any(isinstance(event, ToolStarted) for event in events)
    settlements = [event for event in events if isinstance(event, ToolSettled)]
    results = [event for event in events if isinstance(event, ToolResult)]
    assert len(settlements) == len(results) == 1
    assert settlements[0].outcome.status is ToolStatus.INDETERMINATE
    assert "handler did not run" in results[0].output
    assert "start record may be partial" in results[0].output


@check
def launched_but_unentered_reader_never_starts_after_deadline_settlement():
    import sliceagent.scheduler as scheduler

    original_thread = scheduler.threading.Thread
    release_entry = threading.Event()
    events = []

    class DelayedThread(original_thread):
        def run(self):
            release_entry.wait()
            super().run()

    scheduler.threading.Thread = DelayedThread
    invocation = ToolInvocation("late-entry", "read_file", {"path": "x"}, 0)
    task = ScheduledTool(
        invocation, ToolPurity.PURE_READ,
        lambda: (events.append("handler"),
                 ToolOutcome(invocation, ToolStatus.SUCCEEDED, "late"))[1],
        on_start=lambda: events.append("started"),
    )
    try:
        outcome = run_ordered([task], timeout=T(0.01))
        assert outcome[0].status is ToolStatus.CANCELLED, outcome
        assert events == [], "an unentered call must settle as not-started"
        release_entry.set()
        time.sleep(T(0.05))
        assert events == [], "a settled call must never announce/start later"
    finally:
        release_entry.set()
        scheduler.threading.Thread = original_thread


@check
def timed_read_waits_for_a_concurrent_slot_until_its_own_deadline():
    import sliceagent.scheduler as scheduler

    original_slots = scheduler._TIMEOUT_READER_SLOTS
    occupied = threading.BoundedSemaphore(1)
    assert occupied.acquire(blocking=False)
    scheduler._TIMEOUT_READER_SLOTS = occupied
    # deliberately NOT T(): the slot-wait window it races scales, so a fixed early
    # release widens the margin rather than preserving the flaky 3.3x ratio.
    release_slot = threading.Timer(0.03, occupied.release)
    invocation = ToolInvocation("wait-slot", "read_file", {"path": "x"}, 0)
    release_slot.start()
    try:
        outcome = run_ordered([
            ScheduledTool(
                invocation, ToolPurity.PURE_READ,
                lambda: ToolOutcome(invocation, ToolStatus.SUCCEEDED, "ok"),
            ),
        ], timeout=T(0.2))
        assert outcome[0].status is ToolStatus.SUCCEEDED, outcome
    finally:
        release_slot.cancel()
        release_slot.join(timeout=1)
        scheduler._TIMEOUT_READER_SLOTS = original_slots


@check
def exhausted_reader_slots_settle_without_a_configured_tool_timeout():
    import sliceagent.scheduler as scheduler

    original_slots = scheduler._TIMEOUT_READER_SLOTS
    occupied = threading.BoundedSemaphore(1)
    assert occupied.acquire(blocking=False)
    scheduler._TIMEOUT_READER_SLOTS = occupied
    invocation = ToolInvocation("capacity", "read_file", {"path": "x"}, 0)
    ran = []
    started = time.monotonic()
    try:
        outcome = run_ordered([
            ScheduledTool(
                invocation, ToolPurity.PURE_READ,
                lambda: (ran.append(True), ToolOutcome(
                    invocation, ToolStatus.SUCCEEDED, "unexpected",
                ))[1],
            ),
        ])
        assert time.monotonic() - started < T(0.5)
        assert outcome[0].status is ToolStatus.CANCELLED
        assert "capacity" in outcome[0].text
        assert ran == []
    finally:
        scheduler._TIMEOUT_READER_SLOTS = original_slots
        occupied.release()


@check
def lifecycle_read_does_not_disable_an_adjacent_read_deadline():
    release = threading.Event()
    finished = threading.Event()
    entered = threading.Event()
    returned = threading.Event()
    read_inv = ToolInvocation("safe-timeout", "read_file", {"path": "fifo"}, 0)
    child_inv = ToolInvocation("child", "spawn_agent", {"task": "inspect"}, 1)
    child_ran = []
    box = {}

    def read():
        try:
            entered.set()
            release.wait()
            return ToolOutcome(read_inv, ToolStatus.SUCCEEDED, "late")
        finally:
            finished.set()

    def child():
        child_ran.append(True)
        return ToolOutcome(child_inv, ToolStatus.SUCCEEDED, "child")

    def schedule():
        try:
            box["outcomes"] = run_ordered([
                ScheduledTool(read_inv, ToolPurity.PURE_READ, read, timeout_safe=True),
                ScheduledTool(child_inv, ToolPurity.PURE_READ, child, timeout_safe=False),
            ], timeout=T(0.5))
        except BaseException as error:  # noqa: BLE001 - surfaced on the test thread below
            box["error"] = error
        finally:
            returned.set()

    controller = threading.Thread(target=schedule, daemon=True)
    controller.start()
    try:
        assert entered.wait(T(2)), "ordinary read never crossed the execution boundary"
        assert returned.wait(T(3)), "adjacent lifecycle work disabled the ordinary read deadline"
        assert "error" not in box, box
        outcomes = box["outcomes"]
        assert [outcome.status for outcome in outcomes] == [
            ToolStatus.INDETERMINATE, ToolStatus.CANCELLED,
        ], outcomes
        assert child_ran == []
    finally:
        release.set()
        controller.join(T(1))
        assert not controller.is_alive(), "scheduler controller did not retire after fixture release"
        assert finished.wait(T(1))


@check
def late_indeterminate_read_still_closes_later_effect_barriers():
    read_inv = ToolInvocation("late-unknown", "read_file", {"path": "remote"}, 0)
    edit_inv = ToolInvocation("later-edit", "edit_file", {"path": "x"}, 1)
    entered = threading.Event()
    cutoff = threading.Event()
    return_read = threading.Event()
    returned = threading.Event()
    edits = []
    box = {}

    def uncertain_read():
        entered.set()
        return_read.wait()
        return ToolOutcome(read_inv, ToolStatus.INDETERMINATE, "remote result uncertain")

    def schedule():
        try:
            box["outcomes"] = run_ordered([
                ScheduledTool(
                    read_inv, ToolPurity.PURE_READ, uncertain_read,
                    request_cancel=lambda kind: cutoff.set() if kind == "deadline" else None,
                    cancel_grace=T(3.0),
                ),
                ScheduledTool(
                    edit_inv, ToolPurity.EFFECTFUL,
                    lambda: (edits.append(True), ToolOutcome(
                        edit_inv, ToolStatus.SUCCEEDED, "edited",
                    ))[1],
                ),
            ], timeout=T(0.5))
        except BaseException as error:  # noqa: BLE001 - surfaced on the test thread below
            box["error"] = error
        finally:
            returned.set()

    controller = threading.Thread(target=schedule, daemon=True)
    controller.start()
    try:
        assert entered.wait(T(2)), "read never crossed the execution boundary"
        assert cutoff.wait(T(2)), "read did not cross the configured deadline"
        return_read.set()
        assert returned.wait(T(2)), "late indeterminate result did not settle during its cancellation grace"
        assert "error" not in box, box
        outcomes = box["outcomes"]
        assert [outcome.status for outcome in outcomes] == [
            ToolStatus.INDETERMINATE, ToolStatus.CANCELLED,
        ], outcomes
        assert "remote result uncertain" in outcomes[0].text, outcomes[0]
        assert edits == []
    finally:
        return_read.set()
        controller.join(T(1))
        assert not controller.is_alive(), "scheduler controller did not retire after fixture release"


@check
def blocking_start_publication_times_out_without_entering_handler_or_late_tool_started():
    from sliceagent.events import ToolExecutionStarted, ToolStarted

    gate = threading.Event()
    publication_entered = threading.Event()
    handler_ran = []
    events = []
    invocation = ToolInvocation("slow-start", "read_file", {"path": "x"}, 0)

    def dispatch(event):
        if isinstance(event, ToolExecutionStarted):
            publication_entered.set()
            gate.wait()
        events.append(event)

    class Host:
        def accesses(self, _name, _args):
            from sliceagent.access import ReadAllAccess
            return [ReadAllAccess()]

        def run(self, _name, _args):
            handler_ran.append(True)
            return "unexpected"

    result = []

    def invoke():
        result.extend(run_tool_batch([
            _tc("read_file", {"path": "x"}, invocation.id),
        ], Host(), dispatch, Hooks())[1])

    prior = os.environ.get("AGENT_TOOL_TIMEOUT")
    os.environ["AGENT_TOOL_TIMEOUT"] = str(T(0.02))
    thread = threading.Thread(target=invoke, daemon=True)
    try:
        thread.start()
        assert publication_entered.wait(T(1))
        thread.join(T(0.4))
        assert not thread.is_alive(), "deadline must remain enforceable while start publication blocks"
        assert result[0]["status"] == "indeterminate"
        assert handler_ran == []
        gate.set()
        time.sleep(T(0.05))
        assert not any(isinstance(event, ToolStarted) for event in events), \
            "the guarded start boundary must not publish ToolStarted after settlement"
    finally:
        gate.set()
        thread.join(T(1))
        if prior is None:
            os.environ.pop("AGENT_TOOL_TIMEOUT", None)
        else:
            os.environ["AGENT_TOOL_TIMEOUT"] = prior


@check
def in_flight_tool_started_is_pinned_to_original_dispatch_epoch():
    from sliceagent.access import ReadAllAccess
    from sliceagent.events import ToolStarted, make_dispatcher

    started_edge = threading.Event()
    release_edge = threading.Event()
    old_events, new_events, handler_ran = [], [], []
    route = {"sink": None}

    def old_sink(event):
        if isinstance(event, ToolStarted):
            started_edge.set()
            release_edge.wait()
        old_events.append(event)

    def new_sink(event):
        new_events.append(event)

    route["sink"] = old_sink

    def router(event):
        route["sink"](event)

    def bind_router():
        return route["sink"]

    router.bind_dispatch = bind_router
    dispatch = make_dispatcher(required=(router,))

    class Host:
        def accesses(self, _name, _args):
            return [ReadAllAccess()]

        def run(self, _name, _args):
            handler_ran.append(True)
            return "unexpected"

    prior = os.environ.get("AGENT_TOOL_TIMEOUT")
    os.environ["AGENT_TOOL_TIMEOUT"] = str(T(0.02))
    result = []
    thread = threading.Thread(
        target=lambda: result.extend(run_tool_batch([
            _tc("read_file", {"path": "x"}, "epoch-start"),
        ], Host(), dispatch, Hooks())[1]),
        daemon=True,
    )
    try:
        thread.start()
        assert started_edge.wait(T(1))
        thread.join(T(0.4))
        assert not thread.is_alive() and result[0]["status"] == "indeterminate"
        route["sink"] = new_sink       # simulate a new turn/workspace becoming the router target
        release_edge.set()
        time.sleep(T(0.05))
        assert handler_ran == []
        assert any(isinstance(event, ToolStarted) for event in old_events)
        assert not any(isinstance(event, ToolStarted) for event in new_events), \
            "an admitted edge already in flight must remain pinned to the original dispatch epoch"
    finally:
        release_edge.set()
        thread.join(T(1))
        if prior is None:
            os.environ.pop("AGENT_TOOL_TIMEOUT", None)
        else:
            os.environ["AGENT_TOOL_TIMEOUT"] = prior


@check
def dispatcher_detaches_nested_event_payloads_for_every_sink():
    from sliceagent.events import ToolStarted, make_dispatcher

    invocation = ToolInvocation("detach", "read_file", {"path": "truth"}, 0)
    original = ToolStarted("read_file", {"path": "truth", "nested": {"value": 1}}, invocation)
    observed = []

    def corrupt(event):
        event.args["path"] = "corrupted"
        event.args["nested"]["value"] = 99
        event.invocation.args["path"] = "also-corrupted"

    def observe(event):
        observed.append(event)

    make_dispatcher(corrupt, observe)(original)
    assert observed[0].args == {"path": "truth", "nested": {"value": 1}}
    assert dict(observed[0].invocation.args) == {"path": "truth"}
    assert original.args == {"path": "truth", "nested": {"value": 1}}
    assert dict(original.invocation.args) == {"path": "truth"}


@check
def cancelled_lifecycle_read_wave_caps_abandoned_workers_without_recursive_read_slots():
    import sliceagent.scheduler as scheduler

    original_slots = scheduler._LIFECYCLE_READER_SLOTS
    scheduler._LIFECYCLE_READER_SLOTS = threading.BoundedSemaphore(1)
    release = threading.Event()
    cancel = threading.Event()
    started = []

    def task(index):
        invocation = ToolInvocation(f"life-{index}", "spawn_agent", {"task": str(index)}, index)

        def read():
            started.append(index)
            release.wait()
            return ToolOutcome(invocation, ToolStatus.SUCCEEDED, "late")

        return ScheduledTool(invocation, ToolPurity.PURE_READ, read, timeout_safe=False)

    timer = threading.Timer(0.04, cancel.set)
    timer.start()
    try:
        outcomes = run_ordered([task(0), task(1)], should_cancel=cancel.is_set)
        assert [outcome.status for outcome in outcomes] == [
            ToolStatus.INDETERMINATE, ToolStatus.CANCELLED,
        ]
        assert started == [0], "the lifecycle cap must keep the queued child provably unstarted"
    finally:
        timer.cancel()
        timer.join(timeout=1)
        release.set()
        scheduler._LIFECYCLE_READER_SLOTS = original_slots


@check
def queued_read_never_starts_after_cancellation_cutoff():
    cancel = threading.Event()
    second_started = []
    first_inv = ToolInvocation("first", "read_file", {"path": "first"}, 0)
    second_inv = ToolInvocation("second", "read_file", {"path": "second"}, 1)

    def first():
        cancel.set()
        return ToolOutcome(first_inv, ToolStatus.SUCCEEDED, "first")

    def second():
        second_started.append(True)
        return ToolOutcome(second_inv, ToolStatus.SUCCEEDED, "second")

    outcomes = run_ordered([
        ScheduledTool(first_inv, ToolPurity.PURE_READ, first),
        ScheduledTool(second_inv, ToolPurity.PURE_READ, second),
    ], max_workers=1, timeout=5, should_cancel=cancel.is_set)
    assert [outcome.status for outcome in outcomes] == [ToolStatus.SUCCEEDED, ToolStatus.CANCELLED]
    assert not second_started, "a queued read must not start after cancellation established the cutoff"


@check
def timed_read_worker_cap_cancels_unstarted_calls_without_leaking_threads():
    import sliceagent.scheduler as scheduler

    original_slots = scheduler._TIMEOUT_READER_SLOTS
    scheduler._TIMEOUT_READER_SLOTS = threading.BoundedSemaphore(2)
    release = threading.Event()
    all_finished = threading.Event()
    lock = threading.Lock()
    counts = {"started": 0, "finished": 0}
    ran_effect = []

    def task(index):
        invocation = ToolInvocation(f"cap-{index}", "read_file", {"path": str(index)}, index)

        def read():
            with lock:
                counts["started"] += 1
            try:
                release.wait()
                return ToolOutcome(invocation, ToolStatus.SUCCEEDED, "late")
            finally:
                with lock:
                    counts["finished"] += 1
                    if counts["finished"] == 2:
                        all_finished.set()

        return ScheduledTool(invocation, ToolPurity.PURE_READ, read)

    effect_inv = ToolInvocation("after-cap", "edit_file", {"path": "later"}, 3)

    def effect():
        ran_effect.append(True)
        return ToolOutcome(effect_inv, ToolStatus.SUCCEEDED, "edited")

    try:
        outcomes = run_ordered([
            task(0), task(1), task(2),
            ScheduledTool(effect_inv, ToolPurity.EFFECTFUL, effect),
        ], max_workers=3, timeout=T(0.03))
        statuses = [outcome.status for outcome in outcomes]
        assert statuses[:3].count(ToolStatus.INDETERMINATE) == 2
        assert statuses[:3].count(ToolStatus.CANCELLED) == 1
        assert statuses[3] is ToolStatus.CANCELLED
        assert counts["started"] == 2, "the slot cap must prevent a third daemon reader from starting"
        assert not ran_effect
    finally:
        scheduler._TIMEOUT_READER_SLOTS = original_slots
        release.set()
        assert all_finished.wait(T(1)), "every admitted daemon fixture must release its captured slot"


@check
def lifecycle_preflight_is_resolved_after_each_prior_barrier_settles():
    focus = {"value": "old"}
    observed, ran = [], []

    class BarrierHooks(Hooks):
        def preflight_tool(self, name, _args):
            from sliceagent.hooks import ToolPreflight
            observed.append((name, focus["value"]))
            if name == "second" and focus["value"] != "new":
                return ToolPreflight(True, "lifecycle hook observed stale focus", kind="lifecycle")
            return ToolPreflight()

        def transform_tool_result(self, name, _args, _output):
            if name == "first":
                focus["value"] = "new"

    class Host:
        def accesses(self, _name, _args):
            return []

        def run(self, name, _args):
            ran.append(name)
            return "ok"

    _, results = run_tool_batch(
        [_tc("first", {}, "first"), _tc("second", {}, "second")],
        Host(), lambda _event: None, BarrierHooks(),
    )
    assert ran == ["first", "second"]
    assert observed == [("first", "old"), ("second", "new")], observed
    assert all(result["status"] == "succeeded" for result in results)


@check
def cancellation_after_model_return_prevents_returned_mutation():
    signal = threading.Event()
    ran = []

    class LLM:
        def complete(self, _messages, _schemas):
            signal.set()
            return NS(content="", tool_calls=[_tc("edit_file", {"path": "x"}, "edit")],
                      finish_reason="tool_calls", usage={"prompt_tokens": 2})

    class Host:
        def schemas(self):
            return []

        def accesses(self, _name, _args):
            return []

        def run(self, name, _args):
            ran.append(name)
            return "edited"

    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=LLM(), tools=Host(), dispatch=lambda _event: None, hooks=Hooks(), signal=signal,
    )
    assert result.stop_reason == "aborted" and ran == []
    assert result.usage.prompt_tokens == 2


@check
def cancellation_during_barrier_preparation_prevents_the_effect():
    signal = threading.Event()
    ran = []
    invocation = ToolInvocation("one", "effect", {}, 0)

    def prepare():
        signal.set()
        return None

    outcome = run_ordered([
        ScheduledTool(
            invocation, ToolPurity.EFFECTFUL,
            lambda: (ran.append("effect"), ToolOutcome(invocation, ToolStatus.SUCCEEDED, "ok"))[1],
            prepare=prepare,
        ),
    ], should_cancel=signal.is_set)
    assert ran == []
    assert len(outcome) == 1 and outcome[0].status is ToolStatus.CANCELLED


@check
def cancellation_between_barriers_publishes_cancelled_tail():
    signal = threading.Event()
    ran = []

    class Host:
        def accesses(self, _name, _args):
            return []

        def run(self, name, _args):
            ran.append(name)
            if name == "first_write":
                signal.set()
            return "ok"

    _, results = run_tool_batch([
        _tc("first_write", {}, "one"), _tc("second_write", {}, "two"),
    ], Host(), lambda _event: None, Hooks(), signal=signal)
    assert ran == ["first_write"]
    assert [result["status"] for result in results] == ["succeeded", "cancelled"]


@check
def typed_child_usage_is_aggregated_and_stops_before_another_parent_call():
    from sliceagent.execution import ToolEffect

    class LLM:
        def __init__(self):
            self.calls = 0

        def complete(self, _messages, _schemas):
            self.calls += 1
            if self.calls == 1:
                return NS(content="", tool_calls=[_tc("spawn_agent", {"task": "inspect"}, "child")],
                          finish_reason="tool_calls",
                          usage={"prompt_tokens": 1, "completion_tokens": 1})
            return NS(content="should not happen", tool_calls=[], finish_reason="stop",
                      usage={"prompt_tokens": 1, "completion_tokens": 1})

    class Host:
        def schemas(self):
            return []

        def accesses(self, _name, _args):
            return []

        def run(self, _name, _args):
            return ToolText(
                "child report", effects=(ToolEffect(
                    "child-1:model-usage", "model_usage",
                    {"prompt_tokens": 90, "completion_tokens": 10},
                ),),
            )

    llm = LLM()
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=llm, tools=Host(), dispatch=lambda _event: None,
        hooks=BudgetHook(10), max_steps=3,
    )
    assert result.stop_reason == "token_budget" and llm.calls == 1
    assert result.usage.prompt_tokens == 91 and result.usage.completion_tokens == 11


@check
def parallel_children_receive_lifecycle_metadata_but_no_budget_share():
    from sliceagent.access import ReadAllAccess

    seen = []

    class Host:
        def accesses(self, _name, _args):
            return [ReadAllAccess()]

        def run(self, _name, args):
            seen.append(dict(args))
            return "done"

    budget = BudgetHook(30)
    budget.reset_for_turn()
    budget.record_step_usage({"prompt_tokens": 8, "completion_tokens": 2})
    calls = [
        _tc("spawn_agent", {"agent": "explorer", "task": "one"}, "one"),
        _tc("spawn_agent", {"agent": "explorer", "task": "two"}, "two"),
    ]
    _, results = run_tool_batch(calls, Host(), lambda _event: None, budget)
    assert len(seen) == 2
    # Children are bounded by their step cap and per-child liveness policy. A parent-level budget applies
    # through usage accounting on the parent side; it is never split into per-child shares.
    assert all("__sliceagent_token_budget" not in args for args in seen)
    by_task = {args["task"]: args for args in seen}
    assert by_task["one"][CHILD_INVOCATION_ID_ARG] == "one"
    assert by_task["two"][CHILD_INVOCATION_ID_ARG] == "two"
    assert by_task["one"][CHILD_REQUEST_ORDINAL_ARG] == 1
    assert by_task["two"][CHILD_REQUEST_ORDINAL_ARG] == 2
    assert isinstance(by_task["one"][CHILD_ACTIVITY_ARG], ChildActivity)
    assert isinstance(by_task["two"][CHILD_ACTIVITY_ARG], ChildActivity)
    assert by_task["one"][CHILD_ACTIVITY_ARG] is not by_task["two"][CHILD_ACTIVITY_ARG]
    private = {CHILD_ACTIVITY_ARG, CHILD_INVOCATION_ID_ARG, CHILD_REQUEST_ORDINAL_ARG}
    assert all(not private.intersection(result["args"]) for result in results), \
        "scheduler metadata must not leak into the canonical invocation"


@check
def cancelled_child_still_lets_the_allowed_sibling_run():
    from sliceagent.access import ReadAllAccess
    from sliceagent.hooks import ToolPreflight

    seen = []

    class SelectiveBudget(BudgetHook):
        def preflight_tool(self, _name, args):
            return ToolPreflight(args.get("task") == "cancelled", "cancelled for test", kind="lifecycle")

    class Host:
        def accesses(self, _name, _args):
            return [ReadAllAccess()]

        def run(self, _name, args):
            seen.append(dict(args))
            return "done"

    budget = SelectiveBudget(20)
    calls = [
        _tc("spawn_agent", {"agent": "explorer", "task": "cancelled"}, "cancelled"),
        _tc("spawn_agent", {"agent": "explorer", "task": "allowed"}, "allowed"),
    ]
    _, results = run_tool_batch(calls, Host(), lambda _event: None, budget)
    assert len(seen) == 1 and seen[0]["task"] == "allowed"
    assert results[0]["status"] == "cancelled" and results[1]["status"] == "succeeded"


@check
def registry_rejected_child_does_not_block_a_valid_sibling_same_or_later_wave():
    from sliceagent.access import AllAccess, ReadAllAccess

    for interleaved in (False, True):
        seen = []
        registry = ToolRegistry()
        registry.register(ToolEntry(
            "spawn_agent", {"type": "function", "function": {
                "name": "spawn_agent", "parameters": {
                    "type": "object", "properties": {}, "required": ["task"],
                },
            }}, lambda args: seen.append(dict(args)) or "done",
            accesses=lambda _args: [ReadAllAccess()], purity=ToolPurity.PURE_READ,
        ))
        registry.register(ToolEntry(
            "barrier", {"type": "function", "function": {
                "name": "barrier", "parameters": {},
            }}, lambda _args: "done", accesses=lambda _args: [AllAccess()],
            purity=ToolPurity.EFFECTFUL,
        ))

        class Host:
            def accesses(self, name, args):
                return registry.accesses(name, args)

            def preflight_run(self, name, args):
                return registry.admit(name, args)

            def run_preflighted(self, name, args, admission):
                return registry.run_admitted(admission, args)

            def run(self, name, args):
                return registry.run(name, args)

        calls = [_tc("spawn_agent", {"agent": "explorer"}, "invalid")]
        if interleaved:
            calls.append(_tc("barrier", {}, "barrier"))
        calls.append(_tc(
            "spawn_agent", {"agent": "explorer", "task": "valid"}, "valid",
        ))
        _, rows = run_tool_batch(calls, Host(), lambda _event: None, BudgetHook(20))
        assert len(seen) == 1 and seen[0]["task"] == "valid"
        assert "__sliceagent_token_budget" not in seen[0]
        assert rows[0]["status"] == "failed" and rows[-1]["status"] == "succeeded"


@check
def preflight_counts_schemas_and_output_reserve():
    llm = NS(context_window=70, max_tokens=40)   # 180-byte-era fixture scaled to token units (#33)
    messages = [{"role": "user", "content": "m" * 50}]
    schemas = [{"type": "function", "function": {"name": "x", "description": "s" * 80}}]
    try:
        preflight_model_call(llm, messages, schemas, allow_unknown=False)
        assert False, "strict preflight must reject the over-capacity request"
    except PreflightOverflow as error:
        report = error.report
        assert report.schema_tokens > 0 and report.output_reserve == 40
        assert report.required_tokens > report.context_window


@check
def unknown_window_is_named_compatibility_mode():
    report = preflight_model_call(NS(max_tokens=10), [{"role": "user", "content": "x"}], [],
                                  allow_unknown=True)
    assert report.context_window == 0
    assert report.mode == "compatibility-unknown"


@check
def shared_model_runner_preflights_and_owns_retry_policy():
    import sliceagent.errors as errors

    class LLM:
        context_window = 500
        max_tokens = 20

        def __init__(self):
            self.calls = 0

        @staticmethod
        def is_retryable(_error):
            return True

        def complete(self, _messages, _schemas):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary timeout")
            return "ok"

    llm, events = LLM(), []
    old_sleep = errors.time.sleep
    errors.time.sleep = lambda _delay: None
    try:
        assert complete_model_call(
            llm, [{"role": "user", "content": "small"}], [], dispatch=events.append,
        ) == "ok"
    finally:
        errors.time.sleep = old_sleep
    assert llm.calls == 2
    assert len([event for event in events if isinstance(event, ApiRetry)]) == 1

    llm.context_window = 10
    try:
        complete_model_call(llm, [{"role": "user", "content": "too large"}], [], retry=False,
                            allow_unknown=False)
        assert False, "strict capacity preflight must happen before provider I/O"
    except PreflightOverflow:
        pass
    assert llm.calls == 2


@check
def turn_outcome_keeps_legacy_usage_mapping():
    result = TurnOutcome("end_turn", 2, {"prompt_tokens": 7, "completion_tokens": 3})
    assert result.stop_reason == "end_turn"
    assert isinstance(result.usage, Usage)
    assert result.usage["prompt_tokens"] == 7
    assert dict(result.usage)["completion_tokens"] == 3


@check
def required_pre_dispatch_failure_prevents_tool_execution():
    ran = []
    invocation = ToolInvocation("call-1", "edit_file", {"path": "a.py"}, 0)

    def journal_start():
        raise OSError("journal unavailable")

    task = ScheduledTool(
        invocation, ToolPurity.EFFECTFUL,
        lambda: ran.append(True), on_start=journal_start,
    )
    try:
        run_ordered([task])
        assert False, "an unjournaled effectful call must not run"
    except OSError as exc:
        assert "journal unavailable" in str(exc)
    assert ran == []


@check
def failed_required_reduction_stops_before_next_mutation_barrier():
    ran = []

    class Host:
        def accesses(self, _name, _args):
            return []

        def run(self, name, _args):
            ran.append(name)
            return "ok"

    def dispatch(event):
        from sliceagent.events import ToolResult
        if isinstance(event, ToolResult):
            raise OSError("reducer unavailable")

    calls = [_tc("first_write", {}, "one"), _tc("second_write", {}, "two")]
    try:
        run_tool_batch(calls, Host(), dispatch, Hooks())
        assert False, "the failed first barrier must stop the batch"
    except OSError:
        pass
    assert ran == ["first_write"]


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as error:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {error!r}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    raise SystemExit(1 if failed else 0)


@check
def a_polled_cancel_aborts_a_blocking_shell_wait_and_reaps():
    """FIELD (the review's Family A — worst finding): in the live TUI the turn runs on a worker
    thread, so no real SIGINT can reach it — Ctrl-C only set a cooperative Event that the blocking
    process.wait() never saw, and the user was held for the command's full remaining runtime
    (measured 112s; the cancelled command then completed and the seal read '1/1 succeeded'). The
    sandbox wait now polls the token every 50ms and converts it into the SAME KeyboardInterrupt the
    plain path raises — one reaper, both frontends."""
    from sliceagent.sandbox import LocalSandbox

    box = LocalSandbox(scrub_secrets=False)
    cancel = threading.Event()
    box.cancel_poll = cancel.is_set
    root = tempfile.mkdtemp(prefix="cancel-wait-")
    target = os.path.join(root, "alive.txt")
    threading.Timer(0.4, cancel.set).start()
    start = time.monotonic()
    try:
        box.run(f"sleep 30; echo late > {shlex.quote(target)}", cwd=root, timeout=30)
        raise AssertionError("unreachable: the cancel token was ignored")
    except KeyboardInterrupt:
        pass
    elapsed = time.monotonic() - start
    assert elapsed < 5, f"the polled cancel took {elapsed:.1f}s — the Event still can't reach the wait"
    time.sleep(T(0.3))
    assert not os.path.exists(target), "the reaper must take the process group down with the wait"
    # and with no token bound, the same wait respects only the deadline
    box2 = LocalSandbox(scrub_secrets=False)
    start = time.monotonic()
    code, _ = box2.run("echo ok", cwd=root, timeout=5)
    assert code == 0 and time.monotonic() - start < 5


@check
def a_whole_file_overwrite_is_refused_when_the_file_changed_since_the_read():
    """FIELD (the review's Family E — the only data-loss finding): read_file → a human save (or a
    branch switch) in the generation-time window → edit_file overwrote the file with the pre-read
    body; the agent's own verify passed and the footer read 3/3 succeeded. The guard lives in the
    TOOL, not in model judgement (claude-code's refuse model; Hermes' mtime-warning is advisory and
    too weak): a changed file is refused with a STEERED redirect naming the change."""
    from sliceagent.tools import LocalToolHost

    root = tempfile.mkdtemp(prefix="stale-write-")
    path = os.path.join(root, "calc.py")
    with open(path, "w") as f:
        f.write("def add(a, b):\n    return a + b\n")
    host = LocalToolHost(root=root)
    host.run("read_file", {"path": "calc.py"})
    # the human saves a new function in the read→write window
    with open(path, "a") as f:
        f.write("\n\ndef human_save():\n    return 61\n")
    out = host.run("edit_file", {"path": "calc.py",
                                 "content": "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"})
    assert out.status == ToolStatus.STEERED, out.status
    assert "changed on disk" in str(out) and "/undo" in str(out)
    with open(path) as f:
        assert "human_save" in f.read(), "the human's save must survive the refused write"
    # the prescribed retry — re-read, then the edit lands
    host.run("read_file", {"path": "calc.py"})
    out = host.run("edit_file", {"path": "calc.py", "content": "# merged\n"})
    assert "Wrote" in str(out), str(out)[:120]
    # chained edits keep working: the mark updates after each write
    out = host.run("edit_file", {"path": "calc.py", "content": "# merged v2\n"})
    assert "Wrote" in str(out), str(out)[:120]
    # and a write without any prior read stays allowed (today's contract)
    out = host.run("edit_file", {"path": "fresh.py", "content": "x = 1\n"})
    assert "Wrote" in str(out)


@check
def the_agents_own_str_replace_and_append_do_not_poison_the_staleness_mark():
    """U3 (confirmed in review, then re-confirmed here): str_replace/append wrote the file but
    never refreshed the read mark, so the everyday read_file → str_replace → edit_file sequence
    was REFUSED with 'the file changed on disk since the last read_file' — blaming a disk change
    that was the agent's own edit. Every write path now re-marks, so the guard trips only on an
    EXTERNAL change (which must still be refused)."""
    from sliceagent.tools import LocalToolHost

    root = tempfile.mkdtemp(prefix="self-mark-")
    path = os.path.join(root, "ops.py")
    with open(path, "w") as f:
        f.write("a = 1\nb = 2\n")
    host = LocalToolHost(root=root)
    host.run("read_file", {"path": "ops.py"})
    out = host.run("str_replace", {"path": "ops.py", "old_string": "b = 2", "new_string": "b = 3"})
    assert "Replaced" in str(out), str(out)[:120]
    # the follow-up whole-file write must NOT be refused: nothing external changed
    out = host.run("edit_file", {"path": "ops.py", "content": "a = 1\nb = 3\nc = 4\n"})
    assert "Wrote" in str(out), f"the agent's own str_replace poisoned the mark: {str(out)[:200]}"
    # same for append
    host.run("read_file", {"path": "ops.py"})
    host.run("append_to_file", {"path": "ops.py", "content": "d = 5\n"})
    out = host.run("edit_file", {"path": "ops.py", "content": "# rewritten\n"})
    assert "Wrote" in str(out), f"the agent's own append poisoned the mark: {str(out)[:200]}"
    # and the guard still catches a REAL external change
    host.run("read_file", {"path": "ops.py"})
    with open(path, "a") as f:
        f.write("e = 6\n")
    out = host.run("edit_file", {"path": "ops.py", "content": "# clobber\n"})
    assert out.status == ToolStatus.STEERED, "an external change must still be refused"


@check
def the_streaming_view_counts_a_final_line_without_a_trailing_newline():
    """U5 (confirmed in review, then re-confirmed here): _huge_file_view counted total as the
    b\"\\n\" count, so any file above the 8MB slurp cap whose last line lacks a trailing newline
    (a jq -c dump, a minified bundle, a single-line JSON blob) dropped that line from the view
    and reported the footer one short. The count now follows splitlines() semantics, matching
    the small-file path."""
    from sliceagent.tools import LocalToolHost, _READ_SLURP_CAP

    root = tempfile.mkdtemp(prefix="huge-view-")
    path = os.path.join(root, "dump.jsonl")
    n_filler = (_READ_SLURP_CAP + 4096) // 1025 + 1   # each filler line is exactly 1025 bytes
    with open(path, "w") as f:
        for _ in range(n_filler):
            f.write("x" * 1024 + "\n")
        f.write("LAST-LINE-NO-NEWLINE")               # final line, NO trailing newline
    total = n_filler + 1
    host = LocalToolHost(root=root)
    out = str(host.run("read_file", {"path": "dump.jsonl", "offset": total}))
    assert "LAST-LINE-NO-NEWLINE" in out, "the final newline-less line was dropped from the view"
    assert f"of {total} " in out, f"the footer total is one short: {out[-200:]!r}"
    # the degenerate case: ONE giant line, no newlines at all — the whole file is the last line
    with open(path, "w") as f:
        f.write('{"blob": "' + "y" * (_READ_SLURP_CAP + 100) + '"}')
    out = str(host.run("read_file", {"path": "dump.jsonl", "limit": 1}))
    assert "of 1 " in out, f"a single-line blob must count as one line: {out[-160:]!r}"


@check
def read_file_refuses_a_fifo_instead_of_wedging_the_turn():
    """FIELD (the review's D1): read_file on a FIFO blocked forever — 1702 samples in __open at 60s,
    the turn frozen at 0.1% CPU, and one of 32 reader slots burned permanently. Regular-file types
    are now guarded up front: a FIFO gets a STEERED redirect, never a blocking open."""
    from sliceagent.tools import LocalToolHost
    if os.name == "nt":
        return
    root = tempfile.mkdtemp(prefix="fifo-guard-")
    os.mkfifo(os.path.join(root, "tap"))
    host = LocalToolHost(root=root)
    start = time.monotonic()
    out = host.run("read_file", {"path": "tap"})
    assert time.monotonic() - start < 5, "a FIFO read must fail fast, never block"
    assert out.status == ToolStatus.STEERED and "FIFO" in str(out), str(out)[:160]
    # and regular files still read fine
    with open(os.path.join(root, "a.py"), "w") as f:
        f.write("x = 1\n")
    assert "x = 1" in host.run("read_file", {"path": "a.py"})


@check
def a_huge_file_is_viewed_with_bounded_memory_and_the_same_contract():
    """FIELD (the review's G2): a 159MB file drove RSS ~700MB for a 65KB view — cap-after-buffer.
    Above _READ_SLURP_CAP the view streams: total lines counted in one bounded pass, only the
    requested window materialized, same line-number/footer/offset contract."""
    from sliceagent.tools import LocalToolHost, _READ_SLURP_CAP

    root = tempfile.mkdtemp(prefix="huge-view-")
    path = os.path.join(root, "big.log")
    n_lines = (_READ_SLURP_CAP // 10) + 5000   # ~10 bytes per line, comfortably over the cap
    with open(path, "w") as f:
        for i in range(1, n_lines + 1):
            f.write(f"line-{i:08d}\n")
    host = LocalToolHost(root=root)
    # tracemalloc, not resource.getrusage: `resource` is POSIX-only and broke the Windows job. The
    # peak Python allocation is also the TIGHTER measure here — slurping the file materializes it on
    # the Python heap, which is exactly what tracemalloc counts, while ru_maxrss is a whole-process
    # high-water mark carrying allocator noise from every earlier test in the file.
    import tracemalloc
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()
    out = host.run("read_file", {"path": "big.log"})
    _current, peak = tracemalloc.get_traced_memory()
    if not was_tracing:
        tracemalloc.stop()
    text = str(out)
    assert "line-00000001" in text, text[:200]
    assert f"of {n_lines}" in text, text[-300:]
    assert "memory-bounded streaming read" in text
    assert peak < _READ_SLURP_CAP * 2, (
        f"the view materialized ~{peak / 1e6:.0f}MB for a capped view "
        f"(file is ~{n_lines * 14 / 1e6:.0f}MB, cap is {_READ_SLURP_CAP / 1e6:.0f}MB)")
    # the paging contract holds: a mid-file window lands exactly
    out = host.run("read_file", {"path": "big.log", "offset": n_lines - 10, "limit": 11})
    assert f"line-{n_lines:08d}" in str(out), str(out)[-200:]


@check
def non_recursive_list_files_is_capped_like_the_recursive_branch():
    """FIELD (the review's G3): one uncapped non-recursive list_files injected ~27.5k tokens into a
    turn that reported 1/1 succeeded — the one measured breach of the bounded-slice invariant, nine
    lines from the recursive cap it was missing."""
    from sliceagent.tools import LocalToolHost, _LIST_CAP

    root = tempfile.mkdtemp(prefix="list-cap-")
    for i in range(_LIST_CAP + 50):
        open(os.path.join(root, f"f{i:04d}.txt"), "w").close()
    host = LocalToolHost(root=root)
    out = str(host.run("list_files", {"path": "."}))
    assert f"capped at {_LIST_CAP}" in out, out[-200:]
    assert out.count("\n") < _LIST_CAP + 6, f"uncapped listing ({out.count(chr(10))} lines)"


@check
def an_abandoned_readers_slot_is_released_so_the_session_never_blinds():
    """FIELD (the review's D1 second order): 32 wedged reads permanently consumed every physical
    reader slot — every later read in the session then failed 'reader capacity remained
    unavailable' with no explanation. The wave now frees an abandoned reader's slot at settle, so
    capacity bounds ACTIVE work, not zombies. (Proven by exhaustion: with 31/32 slots pre-held,
    only the WAVE's early release can free one while the reader is still wedged.)"""
    from sliceagent.scheduler import _TIMEOUT_READER_SLOTS

    inv = ToolInvocation("wedged-reader", "read_file", {}, 0)
    gate = threading.Event()

    def wedged():
        gate.wait(T(30))
        return ToolOutcome(inv, ToolStatus.SUCCEEDED, "unblocked")

    held = [_TIMEOUT_READER_SLOTS.acquire(blocking=False) for _ in range(31)]
    assert all(held), "could not pre-fill the reader pool for the exhaustion proof"
    try:
        outcomes = run_ordered([
            ScheduledTool(inv, ToolPurity.PURE_READ, wedged, timeout_safe=True,
                          request_cancel=lambda _k: None),
        ], timeout=T(0.5), lifecycle_absolute=T(5.0))
        assert outcomes[0].status is ToolStatus.INDETERMINATE, outcomes[0].status
        # the wedged thread is STILL blocked — the slot can only be free if the WAVE released it
        acquired = _TIMEOUT_READER_SLOTS.acquire(blocking=False)
        gate.set()
        assert acquired, "the abandoned reader's slot was not released at wave settle"
        _TIMEOUT_READER_SLOTS.release()
    finally:
        gate.set()
        for _ in held:
            _TIMEOUT_READER_SLOTS.release()


@check
def a_running_command_emits_a_byte_evidence_heartbeat():
    """FIELD (the review's Family H): a running command showed nothing but a spinner until exit —
    120s default, 600s ceiling, and a degraded-but-alive command was indistinguishable from a hung
    one. The wait loop now reports the output byte count ~1/s over the presentation-only
    host_activity channel: a growing count names progress, a frozen one names a stall."""
    from sliceagent.tools import LocalToolHost

    root = tempfile.mkdtemp(prefix="heartbeat-")
    beats = []
    host = LocalToolHost(root=root)
    host._verify_notify = beats.append
    out = host.run("run_command",
                   {"command": "for i in 1 2 3; do printf 'tick-%4096s' x; echo; sleep 1.2; done",
                    "timeout": 10})
    assert "tick-" in str(out)
    assert len(beats) >= 2, f"no heartbeat reached the host channel: {beats}"
    sizes = [float(b.split("·")[-1].strip().split(" ")[0]) for b in beats if " KB output" in b]
    assert len(sizes) >= 2 and sizes[-1] > sizes[0], f"the byte count must GROW with output: {beats}"
    # and without the channel, nothing fires (presentation is opt-in)
    host2 = LocalToolHost(root=root)
    out2 = host2.run("run_command", {"command": "echo done", "timeout": 5})
    assert "done" in str(out2)


@check
def the_update_runner_is_bounded_and_stdin_free():
    """D6: `sliceagent update` ran uv with no timeout and the real TTY as stdin — it hung
    indefinitely after one line of output at 0% CPU. The default runner is now bounded and
    stdin-free."""
    import sliceagent.updater as updater
    captured = {}
    real = updater.subprocess.run
    updater.subprocess.run = lambda cmd, **kw: captured.update(kw) or type("R", (), {"returncode": 0})()
    try:
        updater._uv_runner(["uv", "pip", "install", "x"], check=False)
    finally:
        updater.subprocess.run = real
    assert captured.get("timeout") == 600 and captured.get("stdin") is updater.subprocess.DEVNULL, captured


@check
def the_toolbar_shows_spend_not_only_savings():
    """L7: the always-visible money readout showed only '$X saved' — measured $0.0003 displayed on a
    session that cost $0.0064 (21x). The toolbar now shows spend beside savings."""
    from sliceagent.tui import _savings_label
    label = _savings_label({"model": "deepseek-v4-flash", "cost": 0.0064,
                            "saved_cached_tok": 10_000, "saved_dollars": 0.0003})
    assert "spent" in label and "0.0064" in label, label


if __name__ == "__main__":
    main()


