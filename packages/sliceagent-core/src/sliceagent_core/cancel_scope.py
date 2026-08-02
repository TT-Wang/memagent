"""Thread-scoped execution context: the OWNING turn's cancel token and liveness callback.

A turn's cancel token belongs to the turn that owns the thread physically executing the work —
never to a shared attribute on the one sandbox object. The old shape (run_turn assigns
``sandbox.cancel_poll``) had two confirmed critical defects:

* a detached/background child's run_turn OVERWROTE the parent's binding (ScopedSurface delegates
  ``.sandbox`` to the parent's one LocalSandbox), so the child's own cancel edge — its absolute
  timer, a scheduler cutoff, a steer — reaped the PARENT'S in-flight command and parked the
  parent turn indeterminate with nothing naming the cause;
* out-of-order restores across a concurrent fan-out left a stale, already-fired child token in
  the shared slot, so the parent's NEXT command raised KeyboardInterrupt before its shell was
  even spawned.

The scheduler wave binds the owning turn's token on the worker thread that executes the task;
consumers (the sandbox's polled wait) capture it ONCE, at wait start. Threads never share a
slot, so no turn can stomp, redirect, or inherit another turn's cancellation.
"""

import threading

_local = threading.local()


def bind_cancel(token):
    """Bind the owning turn's cancel callable (returns True when cancelled) to the current
    thread. Returns the previous binding so the caller can restore it with unbind_cancel."""
    prev = getattr(_local, "cancel_token", None)
    _local.cancel_token = token
    return prev


def unbind_cancel(prev) -> None:
    _local.cancel_token = prev


def current_cancel():
    """The cancel callable owned by the current thread's turn, or None."""
    return getattr(_local, "cancel_token", None)


def bind_activity(cb):
    """Bind the current thread's liveness callback; returns the previous binding."""
    prev = getattr(_local, "activity_cb", None)
    _local.activity_cb = cb
    return prev


def unbind_activity(prev) -> None:
    _local.activity_cb = prev


def current_activity():
    """The liveness callback owned by the current thread's turn, or None."""
    return getattr(_local, "activity_cb", None)
