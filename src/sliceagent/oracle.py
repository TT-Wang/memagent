"""Oracle implementations — ground-truth verification, independent of retrieval accuracy.

The loop can gate "done" on this so a retrieval miss can't masquerade as completion.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .execution import ToolStatus
from .sandbox import SANDBOX_TIMEOUT, LocalSandbox


@dataclass(frozen=True)
class OracleResult:
    """Typed completion-gate result with tuple-unpacking compatibility."""

    status: ToolStatus
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.SUCCEEDED

    def __iter__(self):
        yield self.ok
        yield self.output


def default_verify_timeout() -> float:
    """Deadline for an acceptance check, from ``AGENT_VERIFY_TIMEOUT`` (seconds), clamped to the same
    600s ceiling the shell tools use.

    It defaults to that ceiling rather than something tighter because a verify command is chosen by the
    PLAN, not by the host: `npm run build` and `pytest` on a real project routinely outlive a two-minute
    budget, and a deadline the plan cannot see and cannot widen turns a slow-but-correct check into a
    permanent ✗. A hang is bounded by the ceiling either way."""
    raw = str(os.environ.get("AGENT_VERIFY_TIMEOUT", "")).strip()
    try:
        v = float(raw)
    except ValueError:
        return 600.0
    return max(1.0, min(v, 600.0)) if v > 0 else 600.0


class CommandOracle:
    """Runs a verification command (e.g. the project's test suite). Pass/fail by exit code."""

    def __init__(self, cmd: str, timeout: float | None = None, *, root: str | None = None):
        self.cmd = cmd
        self.timeout = default_verify_timeout() if timeout is None else timeout
        self.root = os.path.realpath(root or os.getcwd())

    def verify(self) -> OracleResult:
        # Verification inherits the caller environment for compatibility, but shares the same owned
        # process-group lifecycle as command tools. A timeout is still conservatively indeterminate:
        # ordinary descendants are reaped, yet a deliberately detached process cannot be disproved.
        code, output = LocalSandbox(scrub_secrets=False).run(
            self.cmd, cwd=self.root, timeout=self.timeout,
        )
        output = output.strip()
        if code == SANDBOX_TIMEOUT:
            return OracleResult(ToolStatus.INDETERMINATE, output)
        return OracleResult(ToolStatus.SUCCEEDED if code == 0 else ToolStatus.FAILED, output)


class NullOracle:
    def verify(self) -> OracleResult:
        return OracleResult(ToolStatus.SUCCEEDED)
