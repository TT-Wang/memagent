"""BACKGROUND (DETACHED) DELEGATION — the run_in_background model for spawn_agent.

A foreground spawn wave is ONE blocking step: the parent cannot answer a user steer until every
child in the batch settles. A background child instead runs on a manager-owned thread; the spawn
tool returns immediately with a typed ``running`` child outcome, and the finished report re-enters
the parent as a ``PeerMessage`` on the ordinary steer queue — drained at the next step boundary,
rendered under the injection-safe peer envelope, exactly like a mid-turn user steer.

Integrity rules this module keeps:

- delivery is never mid-step and never lost: completions put onto the LIVE turn's queue, stashed
  while the agent is idle, and flushed (oldest-first) into the next turn's queue on attach;
- the detached fan-out is bounded (same spirit as the scheduler's lifecycle wave ceiling) and each
  child carries an absolute leak-guard watchdog (AGENT_DELEGATION_ABSOLUTE, default 3600s);
- child tokens still reach the host's budget hook through the usage sink — a detached child must
  not become invisible spend.
"""
from __future__ import annotations

import collections
import os
import threading

# Detached children bypass the scheduler's worker slots, so the manager enforces its own ceiling.
# Default raised 4 -> 8 (2026-08-03): the loom-app hour-long review spent ~20 min draining 10
# explorers through a width-4 queue — per-child completions staggered 1-2 min apart instead of
# clustering, the fingerprint of queueing, while the 12-concurrent-children stress go/no-go had
# already passed at width 12. Env-tunable for slower providers or constrained hosts.


def _max_background_children() -> int:
    raw = os.environ.get("AGENT_MAX_BACKGROUND_CHILDREN", "").strip()
    try:
        return max(1, int(raw)) if raw else 8
    except ValueError:
        return 8


_MAX_BACKGROUND_CHILDREN = _max_background_children()


def background_absolute_timeout() -> float:
    """Absolute leak guard for one detached child (AGENT_DELEGATION_ABSOLUTE, default 3600s).

    Unlike the scheduler's per-child INACTIVITY window there is no liveness cell arbitration here;
    this is the plain wall-clock backstop after which the child's cancellation lease fires.
    """
    import math
    import os
    raw = os.environ.get("AGENT_DELEGATION_ABSOLUTE", "").strip()
    try:
        v = float(raw)
        return v if math.isfinite(v) and v > 0 else 3600.0
    except ValueError:
        return 3600.0


class BackgroundChildManager:
    """Owns detached child threads and the delivery of their completions to the parent."""

    def __init__(self, *, max_running: int = _MAX_BACKGROUND_CHILDREN):
        self._lock = threading.Lock()
        self._queue = None                     # the live turn's steer queue, while attached
        self._stash: collections.deque = collections.deque()
        self._running = 0
        self._max_running = max(1, int(max_running))
        self._threads: list[threading.Thread] = []
        self._usage_sink = None

    def set_usage_sink(self, sink) -> None:
        """Host seam for child token accounting (e.g. BudgetHook.record_step_usage); None disables."""
        with self._lock:
            self._usage_sink = sink

    def has_capacity(self) -> bool:
        with self._lock:
            return self._running < self._max_running

    def attach(self, q) -> None:
        """Bind the live turn's steer queue; stashed completions flush FIRST — they are older than
        anything the user is about to type."""
        with self._lock:
            self._queue = q
            pending = list(self._stash)
            self._stash.clear()
        for item in pending:
            q.put(item)

    def detach(self, reclaim=()) -> None:
        """Unbind at turn retirement. ``reclaim`` carries items the turn never drained (swept
        leftovers plus anything that landed after the final sweep) so a completion is never
        stranded in a dead queue."""
        with self._lock:
            self._queue = None
            self._stash.extend(reclaim)

    def deliver(self, peer) -> None:
        """Hand a completion to the live turn's queue, or stash it while the agent is idle."""
        with self._lock:
            q = self._queue
            if q is None:
                self._stash.append(peer)
                return
        q.put(peer)

    def account_usage(self, usage: dict) -> None:
        with self._lock:
            sink = self._usage_sink
        if sink is None or not usage:
            return
        try:
            sink(dict(usage))
        except Exception:  # noqa: BLE001 — accounting is advisory; it must never fail the child
            pass

    def start(self, fn, *, name: str) -> bool:
        """Run fn on a daemon thread; False when the detached fan-out ceiling is reached."""
        with self._lock:
            if self._running >= self._max_running:
                return False
            self._running += 1

        def _guarded():
            try:
                fn()
            finally:
                with self._lock:
                    self._running -= 1

        thread = threading.Thread(target=_guarded, name=name, daemon=True)
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]   # prune; no unbounded growth
            self._threads.append(thread)
        thread.start()
        return True


__all__ = ["BackgroundChildManager", "background_absolute_timeout"]
