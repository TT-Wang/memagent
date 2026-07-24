"""FAN-OUT — bounded parallel scoped turns (docs/SUBAGENT-SCOPED-TURN.md §2).

A deliberately DUMB runner: a bounded ``ThreadPoolExecutor`` over ``run_scoped_agent``, one shared
cancel ``Event``, ordered results. Explicitly NOT the scheduler's lifecycle layer — no wave slots, no
0.10s slot waits, no cancellation-lease machinery (the code the mass pre-start cancellations and the
"agents starting forever" freezes lived in). Provider-level backpressure is the transport's own
``_PhysicalCallGate``; the pool cap only bounds thread count.

Invariants:
  * read-only kinds fan out in parallel; WRITABLE kinds serialize through one module lock
    (no worktrees this pass — two concurrent writers would race the workspace);
  * depth 1 is free — the child's allowed surface never contains a spawn tool;
  * a child that raises is a typed ``failed`` result, never a fan-out crash;
  * results come back in submission order, each slot always filled.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .agents import BUILTIN_AGENTS
from .scoped_agent import ScopedResult, allowed_for, run_scoped_agent

# One writer at a time, process-wide: the workspace is a single shared mutable resource and this pass
# deliberately has no worktree isolation (spec non-goal). Readers never touch this lock.
_WRITER_LOCK = threading.Lock()


@dataclass(frozen=True)
class FanoutTask:
    """One delegation: a kind (BUILTIN_AGENTS key) + a self-contained task brief."""

    task: str
    kind: str = "explorer"
    max_steps: int = 40


@dataclass
class FanoutOutcome:
    """One slot of the ordered fan-out result."""

    index: int
    kind: str
    result: ScopedResult = field(default_factory=ScopedResult)


def run_fanout(tasks, *, tools, llm, retriever, memory, cancel: threading.Event | None = None,
               max_workers: int = 4, model_id: str = "", on_event=None) -> list[FanoutOutcome]:
    """Run every FanoutTask as a scoped turn; return outcomes in submission order.

    ``cancel`` (shared) aborts all children between steps AND mid-stream (run_turn wires
    ``signal.is_set`` into the transport's should_cancel). ``on_event(index, phase)`` receives
    ``starting → running → ok|partial|failed|cancelled`` — enough for a TUI matrix, nothing more.
    """
    cancel = cancel if cancel is not None else threading.Event()
    tasks = list(tasks)

    def _emit(index: int, phase: str) -> None:
        if on_event is not None:
            try:
                on_event(index, phase)
            except Exception:  # noqa: BLE001 — presentation must never kill a child
                pass

    def _one(index: int, ft: FanoutTask) -> FanoutOutcome:
        spec = BUILTIN_AGENTS.get(ft.kind) or BUILTIN_AGENTS["explorer"]
        _emit(index, "starting")
        started = threading.Event()

        def _observe(_event) -> None:
            if not started.is_set():
                started.set()
                _emit(index, "running")

        def _run() -> ScopedResult:
            return run_scoped_agent(
                ft.task, tools=tools, llm=llm, retriever=retriever, memory=memory,
                allowed_tools=allowed_for(spec, tools), model_id=model_id,
                max_steps=ft.max_steps, signal=cancel, reasoning=spec.reasoning or "",
                system_extra=spec.system_prompt, on_event=_observe,
            )

        try:
            if spec.read_only:
                result = _run()
            else:
                with _WRITER_LOCK:
                    # A cancel raised while queued for the writer lock must not start a doomed child.
                    result = ScopedResult(status="cancelled", stop_reason="aborted") \
                        if cancel.is_set() else _run()
        except Exception as exc:  # noqa: BLE001 — a child crash is a typed slot, never a fan-out crash
            result = ScopedResult(status="failed", stop_reason="error",
                                  report=f"child crashed before reporting: {type(exc).__name__}: {exc}")
        _emit(index, result.status)
        return FanoutOutcome(index=index, kind=ft.kind, result=result)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = [pool.submit(_one, i, ft) for i, ft in enumerate(tasks)]
        return [f.result() for f in futures]
