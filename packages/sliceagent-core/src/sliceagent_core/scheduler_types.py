"""Core-owned scheduler values shared across the scheduler port boundary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .execution import ToolInvocation, ToolOutcome, ToolPurity


DEFAULT_LIFECYCLE_ABSOLUTE = 3600.0


@dataclass(frozen=True)
class ScheduledTool:
    invocation: ToolInvocation
    purity: ToolPurity
    run: Callable[[], ToolOutcome]
    on_start: Callable[[], None] | None = None
    timeout_safe: bool = True
    prepare: Callable[[], ToolOutcome | None] | None = None
    # Production dispatch uses this lease-aware form. A deadline may expire while a required journal is
    # blocked; the callback can then stop before emitting the next lifecycle edge, and the handler never runs.
    on_start_guarded: Callable[[Callable[[], bool]], None] | None = None
    # Presentation-only admission signal. It never means the handler started and failures are isolated: queue
    # visibility must not become a new execution/journal gate.
    on_queued: Callable[[str], None] | None = None
    # Lifecycle children have a cancellable provider/tool loop behind this scheduler worker. The scheduler
    # signals only tasks that crossed (or may have partially crossed) the start boundary, then waits the
    # task-declared bounded close grace before deciding timed-out-vs-indeterminate truth.
    request_cancel: Callable[[str], None] | None = None
    cancel_grace: float = 0.0
    # Per-child monotonic liveness cell. Only lifecycle waves interpret it; ordinary reads retain their
    # existing fixed deadline semantics.
    activity: object | None = None


__all__ = ["DEFAULT_LIFECYCLE_ABSOLUTE", "ScheduledTool"]
