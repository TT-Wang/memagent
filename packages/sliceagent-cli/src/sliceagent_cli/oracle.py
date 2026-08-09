"""Oracle implementations — ground-truth verification, independent of retrieval accuracy.

The loop can gate "done" on this so a retrieval miss can't masquerade as completion.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from sliceagent_core.execution import ToolStatus
from .safeguards import catastrophic_reason
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
    """Deadline for an acceptance check, from ``AGENT_VERIFY_TIMEOUT`` (seconds). Default 600s; an
    explicit operator value may raise it up to a 3600s ceiling.

    It defaults to the shell tools' 600s ceiling rather than something tighter because a verify command
    is chosen by the PLAN, not by the host: `npm run build` and `pytest` on a real project routinely
    outlive a two-minute budget, and a deadline the plan cannot see and cannot widen turns a
    slow-but-correct check into a permanent ✗. The old hard 600s clamp was itself a correctness bug the
    other way (#33 review): a real 15–30 min integration suite could NEVER produce a green receipt, only
    INDETERMINATE — so an explicit operator setting now wins up to an hour. A hang is bounded either way."""
    raw = str(os.environ.get("AGENT_VERIFY_TIMEOUT", "")).strip()
    try:
        v = float(raw)
    except ValueError:
        return 600.0
    return max(1.0, min(v, 3600.0)) if v > 0 else 600.0


class CommandOracle:
    """Runs a verification command (e.g. the project's test suite). Pass/fail by exit code."""

    def __init__(self, cmd: str, timeout: float | None = None, *, root: str | None = None,
                 sandbox=None, scrub_secrets: bool = True):
        self.cmd = cmd
        self.timeout = default_verify_timeout() if timeout is None else timeout
        self.root = os.path.realpath(root or os.getcwd())
        # Verification runs through the HOST's configured sandbox (so AGENT_SANDBOX=docker is honored,
        # not silently bypassed) with secret scrubbing ON by default — a model-authored verify command
        # is no more trusted than any other model command, and its output lands in the durable turn
        # artifact. ``scrub_secrets=False`` remains the explicit opt-out for embedders whose operator
        # hook genuinely needs the full environment (review M2).
        self.sandbox = sandbox if sandbox is not None else LocalSandbox(scrub_secrets=scrub_secrets)

    def verify(self) -> OracleResult:
        # Verification shares the same owned process-group lifecycle as command tools. A timeout is
        # still conservatively indeterminate: ordinary descendants are reaped, yet a deliberately
        # detached process cannot be disproved.
        # CATASTROPHE GATE (counter-review M2): a verify command is shell SEMANTICS, not a trusted
        # tool name — classify the command body through the same catastrophic floor every shell
        # tool passes at preflight (H1), BEFORE it reaches the sandbox. update_work acceptance
        # checks and completion-hook verify commands are model-authored shell: they used to skip
        # the gate entirely, so H1 welded the front door while this side door stayed open.
        # INDETERMINATE, not FAILED: the check never ran, so there is no verdict on the work —
        # and the no-verdict path tells the model to fix the CHECK, never to re-edit good work.
        reason = catastrophic_reason("run_command", {"command": self.cmd})
        if reason is not None:
            return OracleResult(
                ToolStatus.INDETERMINATE,
                f"refused by the catastrophic-command safeguard ({reason}); the check never ran — "
                "give the item a non-destructive verify command")
        # A command whose program is not on PATH answers 127, which is indistinguishable from a real
        # red check — the completion gate would then demand code fixes forever because the CHECKER was
        # never installed (an unbounded fix loop on correct work). Same guard as the update_work
        # verify path (tools._run_verify_command): resolve first, report the NO-VERDICT class.
        from .tools import _unrunnable_verify_program  # lazy: tools imports this module lazily too
        missing = _unrunnable_verify_program(self.cmd)
        if missing:
            return OracleResult(ToolStatus.INDETERMINATE,
                                f"{missing!r} is not on PATH; the verification never ran")
        code, output = self.sandbox.run(
            self.cmd, cwd=self.root, timeout=self.timeout,
        )
        output = output.strip()
        if code == SANDBOX_TIMEOUT:
            return OracleResult(ToolStatus.INDETERMINATE, output)
        return OracleResult(ToolStatus.SUCCEEDED if code == 0 else ToolStatus.FAILED, output)


class NullOracle:
    def verify(self) -> OracleResult:
        return OracleResult(ToolStatus.SUCCEEDED)
