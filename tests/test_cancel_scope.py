"""Cancel-token ownership (the review's criticals 1&2): a turn's cancel token belongs to the
thread executing ITS work, never to a shared attribute on the one sandbox object.

No model, no pytest. Run: PYTHONPATH=src python tests/test_cancel_scope.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent import cancel_scope  # noqa: E402
from sliceagent.execution import (ToolInvocation, ToolOutcome, ToolPurity,  # noqa: E402
                                  ToolStatus)
from sliceagent.sandbox import LocalSandbox  # noqa: E402
from sliceagent.scheduler import ScheduledTool, run_ordered  # noqa: E402

CHECKS = []
def check(fn):
    CHECKS.append(fn)
    return fn


def _run_in_thread(fn):
    """Run fn on a daemon thread; return (elapsed, error box) after join."""
    box = {"error": None}
    start = time.monotonic()
    def body():
        try:
            fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e
    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, box, start


@check
def a_childs_cancel_edge_cannot_reap_the_parents_in_flight_command():
    """CRITICAL 1: parent runs a command with ITS token bound on ITS thread; a concurrent child
    binds and FIRES its own token on another thread. The parent's command must survive. On the
    old shared-attribute design the child's binding overwrote the parent's and its cancel edge
    reaped the parent's command mid-flight."""
    sandbox = LocalSandbox()
    parent_fired = threading.Event()
    child_fired = threading.Event()
    parent_done = threading.Event()
    parent_error = {}

    def parent():
        prev = cancel_scope.bind_cancel(parent_fired.is_set)
        try:
            sandbox.run("sleep 5", cwd=tempfile.gettempdir(), timeout=5)
        except BaseException as e:  # noqa: BLE001
            parent_error["e"] = e
        finally:
            cancel_scope.unbind_cancel(prev)
            parent_done.set()

    def child():
        prev = cancel_scope.bind_cancel(child_fired.is_set)
        try:
            # Simulate the old run_turn collision: the child ALSO stomps the shared attribute
            # (exactly what loop.py did before this fix). The fix must make the stomp harmless —
            # the parent's wait captured ITS token at wait start and never re-reads the slot.
            sandbox.cancel_poll = child_fired.is_set
            child_fired.set()          # the child's own cancel edge fires…
            time.sleep(0.3)            # …well inside the parent's in-flight wait
        finally:
            cancel_scope.unbind_cancel(prev)
            sandbox.cancel_poll = None

    start = time.monotonic()
    pt = threading.Thread(target=parent, daemon=True)
    pt.start()
    time.sleep(0.2)                  # let the parent's wait actually start
    ct = threading.Thread(target=child, daemon=True)
    ct.start()
    ct.join(5)
    time.sleep(0.5)                  # several 50ms poll ticks past the child's cancel edge
    assert pt.is_alive(), (
        f"the child's cancel edge reaped the PARENT'S command "
        f"({parent_error.get('e')!r}) — the shared-slot bug is back")
    parent_fired.set()               # now the PARENT's own edge must still work
    pt.join(5)
    elapsed = time.monotonic() - start
    assert not pt.is_alive(), "the parent's own cancel edge no longer reaches its wait"
    assert isinstance(parent_error.get("e"), KeyboardInterrupt), parent_error.get("e")
    assert elapsed < 5, f"cancel conversion took {elapsed:.1f}s"


@check
def out_of_order_turn_retirement_cannot_leave_a_stale_fired_token():
    """CRITICAL 2: two turns overlap (a fan-out); they retire OUT OF ORDER. Afterwards a fresh
    command must NOT see a stale, already-fired token — the old attribute restore (A exits, then
    B exits) left B's fired token in the slot, killing the next command before its shell
    spawned. Thread-scoped bindings die with their thread by construction."""
    sandbox = LocalSandbox()
    fired_a = threading.Event()
    fired_b = threading.Event()
    errors = {}

    def turn(name, fired):
        prev = cancel_scope.bind_cancel(fired.is_set)
        try:
            time.sleep(0.15 if name == "a" else 0.3)   # a retires first, b second
        finally:
            cancel_scope.unbind_cancel(prev)

    ta = threading.Thread(target=turn, args=("a", fired_a), daemon=True)
    tb = threading.Thread(target=turn, args=("b", fired_b), daemon=True)
    ta.start(); tb.start()
    fired_a.set(); fired_b.set()                        # both tokens end up FIRED
    ta.join(5); tb.join(5)
    # The next command on a fresh thread has no binding at all — a stale fired token from a
    # retired sibling must not be reachable.
    def fresh():
        try:
            code, out = sandbox.run("echo alive", cwd=tempfile.gettempdir(), timeout=5)
            errors["result"] = (code, out.strip())
        except BaseException as e:  # noqa: BLE001
            errors["e"] = e
    ft = threading.Thread(target=fresh, daemon=True)
    ft.start(); ft.join(6)
    assert not ft.is_alive(), "a stale token wedged the fresh command's wait"
    assert "e" not in errors, (
        f"a stale fired token killed the fresh command: {errors.get('e')!r}")
    assert errors.get("result") == (0, "alive"), errors


@check
def the_wave_binds_the_owning_turns_token_on_the_worker_thread():
    """The authoritative channel: a task executed by run_ordered sees ITS wave's should_cancel
    through cancel_scope on the executing thread. Remove the bind in scheduler.worker and this
    goes red — and with it the whole live-UI abort chain."""
    seen = {}
    fired = threading.Event()

    def task_body():
        seen["token"] = cancel_scope.current_cancel()
        return ToolOutcome(
            ToolInvocation("t1", "probe", {}, 0), ToolStatus.OK, "done")

    task = ScheduledTool(
        invocation=ToolInvocation("t1", "probe", {}, 0),
        purity=ToolPurity.EFFECTFUL, run=task_body)
    run_ordered([task], should_cancel=fired.is_set)
    assert seen.get("token") is not None, "the worker ran with no cancel token bound"
    assert seen["token"] == fired.is_set or getattr(seen["token"], "__func__", None) == fired.is_set, \
        f"the worker saw a FOREIGN token: {seen['token']!r}"
    # …and the binding was restored after the wave: no residue on the calling thread.
    assert cancel_scope.current_cancel() is None, "the barrier binding leaked past the wave"


@check
def direct_sandbox_users_keep_the_attribute_fallback():
    """Backward compatibility: a direct sandbox user with no wave binding can still arm the
    polled wait through the cancel_poll attribute (test_execution_kernel relies on this)."""
    sandbox = LocalSandbox()
    fired = threading.Event()
    threading.Timer(0.3, fired.set).start()
    sandbox.cancel_poll = fired.is_set
    start = time.monotonic()
    try:
        sandbox.run("sleep 30", cwd=tempfile.gettempdir(), timeout=30)
        raise AssertionError("the attribute fallback no longer cancels the wait")
    except KeyboardInterrupt:
        pass
    finally:
        sandbox.cancel_poll = None
    assert time.monotonic() - start < 5, "the attribute fallback became slow"


if __name__ == "__main__":
    ok = 0
    for fn in CHECKS:
        try:
            fn()
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(CHECKS)} passed")
    sys.exit(0 if ok == len(CHECKS) else 1)
