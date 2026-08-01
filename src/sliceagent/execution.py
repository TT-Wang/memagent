"""Typed execution contracts shared by the loop, registry, and scheduler.

The provider wire format and the legacy event/result dictionaries are projections of
these values.  In particular, tool status is data: output wording never decides whether
an invocation succeeded once it has crossed the typed registry boundary.
"""
from __future__ import annotations

import json
import math
import os
import posixpath
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Callable, Iterator

from .context_overflow import ContextOverflow

if TYPE_CHECKING:  # forward refs only; interfaces must not import execution at runtime
    from .interfaces import PeerParkControl, PeerWait


# Private scheduler→child cancellation lease. Unlike provider/audit arguments this is a live process object;
# run_tool_batch injects it only into the sanitized handler view and ScopedSpawnHost consumes it at the edge.
CHILD_CANCEL_SIGNAL_ARG = "__sliceagent_cancel_signal"
# Stable presentation correlation for concurrent delegation.  These values never enter the provider-visible
# invocation or a child brief; they let the progress reducer bind one physical spawn call to exactly one row.
CHILD_INVOCATION_ID_ARG = "__sliceagent_invocation_id"
CHILD_REQUEST_ORDINAL_ARG = "__sliceagent_request_ordinal"
CHILD_ACTIVITY_ARG = "__sliceagent_activity"
# ScopedSurface → run_command handler: the caller's surface denies the proc_* family, so a
# timeout must REAP, never adopt — adoption would hand the model a follow handle it cannot use
# and leave the process alive, followable by no one (Family J at a new site). Injected into a
# private copy of the args inside the surface, downstream of every audit/journal projection.
NO_ADOPT_ON_TIMEOUT_ARG = "__sliceagent_no_adopt_on_timeout"
# Delegation is a lifecycle class, not four unrelated string comparisons. A host that introduces another
# delegation tool registers its name here once; scheduling leases, reconciliation, and receipt accounting then
# move together instead of silently disagreeing.
DELEGATION_TOOL_NAMES = frozenset({"spawn_agent"})


def is_delegation_tool(name: object) -> bool:
    return str(name or "") in DELEGATION_TOOL_NAMES


class ChildActivity:
    """Thread-safe monotonic liveness cell shared by one parent scheduler and one child."""

    def __init__(self, admitted_at: float | None = None):
        self._lock = threading.Lock()
        self._last = time.monotonic() if admitted_at is None else float(admitted_at)

    @property
    def last(self) -> float:
        with self._lock:
            return self._last

    def touch(self, at: float | None = None) -> None:
        observed = time.monotonic() if at is None else float(at)
        with self._lock:
            self._last = max(self._last, observed)


class ToolStatus(str, Enum):
    SUCCEEDED = "succeeded"
    STEERED = "steered"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"

    @property
    def conclusive(self) -> bool:
        """Whether execution reached a terminal state with no unresolved effects."""
        return self is not ToolStatus.INDETERMINATE

    @property
    def failing(self) -> bool:
        """Whether the outcome is adverse rather than a successful or benign steer."""
        return self in {
            ToolStatus.FAILED, ToolStatus.CANCELLED, ToolStatus.INDETERMINATE,
        }


class ToolPurity(str, Enum):
    """Ordering class, deliberately separate from deduplication/idempotence."""

    PURE_READ = "pure_read"
    EFFECTFUL = "effectful"
    UNKNOWN = "unknown"


def _scope_path(value: object) -> str:
    path = str(value or "").replace("\\", "/").strip()
    if not path:
        return "."
    normalized = posixpath.normpath(path)
    return normalized[2:] if normalized.startswith("./") else normalized


def reconciliation_targets(name: str, args: Mapping[str, object] | None) -> tuple[str, ...]:
    """Conservative affected-resource identities for an operation that did not settle conclusively.

    These identities support truthful later re-observation; they are evidence metadata, not an
    execution constraint. A read-only explorer can leave an unknown answer, but cannot leave a workspace
    side effect to reconcile.
    """
    name = str(name or "")
    args = args if isinstance(args, Mapping) else {}
    if is_delegation_tool(name) and str(args.get("agent") or "").casefold() == "explorer":
        return ()
    if name.startswith("mcp__"):
        # MCP methods have no trustworthy common effect schema. A nominal database/network method may also
        # write files through its server process (or vice versa), so an interrupted call must retain every
        # relevant boundary until the user and live workspace state have both been re-observed.
        return ("workspace:*", f"opaque:{name}", f"external:{name}")
    if name in {"fetch_url", "web_search", "ask_user"}:
        return (f"external:{name}",)
    if name in {"proc_poll", "proc_tail", "proc_wait", "proc_kill"} and args.get("handle") is not None:
        return (f"process:{args.get('handle')}",)
    if name in {"terminal_send", "terminal_read", "terminal_wait", "terminal_close"}:
        return (f"terminal:{args.get('session') or 'main'}",)
    if name in {"read_file", "list_files", "edit_file", "append_to_file", "str_replace", "grep", "glob"}:
        return (f"path:{_scope_path(args.get('path') or '.')}",)
    # A shell/code/extension/child call without a declared resource can affect both local and non-local state.
    # Tool semantics deliberately precede incidental argument names: run_command(..., path="README.md") and
    # an unknown extension with a `path` field remain opaque. Do not let provider-added keys narrow real reach.
    return ("workspace:*", f"opaque:{name or 'unknown'}")


@dataclass(frozen=True)
class ToolInvocation:
    id: str
    name: str
    args: Mapping[str, object]
    provider_index: int


@dataclass(frozen=True)
class ToolEffect:
    id: str
    kind: str
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolOutcome:
    invocation: ToolInvocation
    status: ToolStatus
    text: str
    effects: tuple[ToolEffect, ...] = ()
    # Optional TYPED control signal a host tool may return to end the turn (task #101 peer
    # park). Deliberately not text: the loop must never infer control flow from prose. None
    # for every ordinary tool, so existing consumers and as_legacy() are unchanged — the loop
    # reaches it through the already-exposed "outcome" row entry.
    control: "PeerParkControl | None" = None

    def __post_init__(self) -> None:
        if self.control is not None:
            # Exact type only. A park suspends the turn, so an arbitrary object here would be
            # control flow with nothing to resume against.
            from .interfaces import PeerParkControl as _PPC
            if not isinstance(self.control, _PPC):
                raise ValueError("ToolOutcome.control must be a typed PeerParkControl")

    @property
    def failing(self) -> bool:
        return self.status.failing

    def with_text(self, text: object) -> "ToolOutcome":
        """Change presentation only; status/effects remain authoritative."""
        return replace(self, text="" if text is None else str(text))

    def as_legacy(self) -> dict:
        """Compatibility projection consumed by the existing provider/event path."""
        return {
            "id": self.invocation.id,
            "name": self.invocation.name,
            "args": dict(self.invocation.args),
            "output": self.text,
            "failing": self.failing,
            "status": self.status.value,
            "outcome": self,
        }


class TurnStatus(str, Enum):
    END_TURN = "end_turn"
    ABORTED = "aborted"
    MAX_STEPS = "max_steps"
    TOKEN_BUDGET = "token_budget"
    BLOCKED = "blocked"
    STUCK = "stuck"
    OVERFLOW = "overflow"
    MAX_TOKENS = "max_tokens"
    FILTERED = "filtered"
    ERROR = "error"
    INDETERMINATE = "indeterminate"
    # The turn ended parked on a peer (task #101). Typed rather than a loose string so the
    # status/peer_wait biconditional can be enforced at the result boundary.
    WAITING_PEER = "waiting_peer"


@dataclass(frozen=True)
class Usage(Mapping[str, int | float]):
    """Provider-neutral token accounting with a legacy mapping view."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    input_other: int = 0
    input_cache_read: int = 0
    input_cache_creation: int = 0
    output: int = 0
    cost_usd: float | None = None
    # Moat counters (owner's h2h fixes): peak SINGLE-CALL context window (max, not additive) and the
    # apple-to-apple model-call count. First-class fields so the sealed turn record carries them —
    # the TurnEnd EVENT dict alone never reaches the seal, which reads this object.
    peak_call_input: int = 0
    model_calls: int = 0

    def __post_init__(self) -> None:
        # Provider accounting is untrusted telemetry. Negative or malformed counters must never refund a
        # turn's real usage and evade the host budget; non-finite cost values must not poison aggregation.
        for name in (
            "prompt_tokens", "completion_tokens", "input_other",
            "input_cache_read", "input_cache_creation", "output",
            "peak_call_input", "model_calls",
        ):
            try:
                value = max(0, int(getattr(self, name) or 0))
            except (TypeError, ValueError, OverflowError):
                value = 0
            object.__setattr__(self, name, value)
        try:
            cost = float(self.cost_usd) if self.cost_usd is not None else None
        except (TypeError, ValueError, OverflowError):
            cost = None
        object.__setattr__(
            self, "cost_usd",
            cost if cost is not None and math.isfinite(cost) and cost >= 0 else None,
        )
        typed_input = self.input_other + self.input_cache_read + self.input_cache_creation
        if self.prompt_tokens == 0 and typed_input:
            object.__setattr__(self, "prompt_tokens", typed_input)
        if self.completion_tokens == 0 and self.output:
            object.__setattr__(self, "completion_tokens", self.output)
        if self.output == 0 and self.completion_tokens:
            object.__setattr__(self, "output", self.completion_tokens)

    @classmethod
    def from_value(cls, value: "Usage | Mapping[str, object] | None") -> "Usage":
        if isinstance(value, cls):
            return value
        data = value or {}

        def integer(key: str) -> int:
            try:
                return int(data.get(key, 0) or 0)
            except (TypeError, ValueError, OverflowError):
                return 0

        cost = data.get("cost_usd")
        return cls(
            prompt_tokens=integer("prompt_tokens"),
            completion_tokens=integer("completion_tokens"),
            input_other=integer("input_other"),
            input_cache_read=integer("input_cache_read"),
            input_cache_creation=integer("input_cache_creation"),
            output=integer("output"),
            cost_usd=cost,
            peak_call_input=integer("peak_call_input"),
            model_calls=integer("model_calls"),
        )

    def as_dict(self) -> dict[str, int | float]:
        out: dict[str, int | float] = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "input_other": self.input_other,
            "input_cache_read": self.input_cache_read,
            "input_cache_creation": self.input_cache_creation,
            "output": self.output,
        }
        if self.input_cache_read:
            out["cached_tokens"] = self.input_cache_read
        if self.cost_usd is not None:
            out["cost_usd"] = self.cost_usd
        out["peak_call_input"] = self.peak_call_input
        out["model_calls"] = self.model_calls
        return out

    def __getitem__(self, key: str) -> int | float:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())

    def __add__(self, other: "Usage | Mapping[str, object]") -> "Usage":
        rhs = Usage.from_value(other)
        cost = None if self.cost_usd is None and rhs.cost_usd is None else (self.cost_usd or 0) + (rhs.cost_usd or 0)
        return Usage(
            prompt_tokens=self.prompt_tokens + rhs.prompt_tokens,
            completion_tokens=self.completion_tokens + rhs.completion_tokens,
            input_other=self.input_other + rhs.input_other,
            input_cache_read=self.input_cache_read + rhs.input_cache_read,
            input_cache_creation=self.input_cache_creation + rhs.input_cache_creation,
            output=self.output + rhs.output,
            cost_usd=cost,
            peak_call_input=max(self.peak_call_input, rhs.peak_call_input),  # a PEAK, never a sum
            model_calls=self.model_calls + rhs.model_calls,
        )


@dataclass(frozen=True)
class TurnOutcome:
    status: TurnStatus | str
    steps: int
    usage: Usage | Mapping[str, object]
    message: str | None = None
    # Typed wrapper-facing provenance for an unexpected stop. Empty for ordinary lifecycle stops. This lets a
    # one-shot child recover a provider-call timeout without mistaking a tool/reducer/setup TimeoutError for one.
    error_origin: str = ""
    # Machine-readable failure shape within that origin.  In particular, ``indeterminate_model_call`` means a
    # watchdog returned while provider I/O may remain live, so wrappers must not launch a recovery request.
    error_kind: str = ""
    # Steers still queued when the turn retired, as (text, admission_id) pairs. These were NEVER delivered to
    # the model and NO SteerDelivered receipt was emitted for them — the caller owns their fate (typically:
    # admit as the next turn's input). A durable host must not treat turn retirement as having consumed them.
    leftover_steers: tuple = ()
    # Set when the turn ended PARKED on a peer (stop_reason "waiting_peer"). Carries the typed
    # PeerWait so the host's seal can persist the park durably. None for every ordinary stop, so
    # existing consumers are unchanged. The paired invariant — status WAITING_PEER iff this is
    # set — is enforced below AND again by the work graph at the durable seal.
    peer_wait: "PeerWait | None" = None

    def __post_init__(self) -> None:
        try:
            status = self.status if isinstance(self.status, TurnStatus) else TurnStatus(str(self.status))
        except ValueError:
            status = str(self.status)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "usage", Usage.from_value(self.usage))
        # Paired invariant at the RESULT boundary, mirroring the work graph's own rule: a turn is
        # parked exactly when it carries the typed wait. Either half alone is a lie — a
        # waiting_peer status with no wait cannot be resumed, and a wait on a finished turn would
        # have the host persist a park for work that already ended.
        parked = status is TurnStatus.WAITING_PEER
        if parked != (self.peer_wait is not None):
            raise ValueError(
                "TurnOutcome: status WAITING_PEER and peer_wait must be present together"
            )
        if parked:
            # Presence alone is not the invariant: an arbitrary object would satisfy "not None"
            # while carrying no correlation to resume against. Require the exact typed wait.
            from .interfaces import PeerWait as _PeerWait
            if not isinstance(self.peer_wait, _PeerWait):
                raise ValueError("TurnOutcome.peer_wait must be a typed PeerWait")

    @property
    def stop_reason(self) -> str:
        return self.status.value if isinstance(self.status, TurnStatus) else str(self.status)


@dataclass(frozen=True)
class PreflightReport:
    estimated_input_tokens: int
    schema_tokens: int
    output_reserve: int
    context_window: int
    mode: str                         # strict | compatibility-unknown

    @property
    def required_tokens(self) -> int:
        return self.estimated_input_tokens + self.schema_tokens + self.output_reserve


class PreflightOverflow(ContextOverflow):
    """A local capacity rejection shaped like provider overflow for existing recovery."""

    def __init__(self, report: PreflightReport):
        self.report = report
        super().__init__(ValueError(
            f"model context window preflight failed: need {report.required_tokens} tokens "
            f"including output reserve, window={report.context_window}"))


class UnknownContextWindow(RuntimeError):
    pass


def _positive_int(value: object) -> int:
    try:
        result = int(value or 0)
        return result if result > 0 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _context_window(llm) -> int:
    """Explicit runtime configuration wins; catalog values are used when genuinely known."""
    configured = _positive_int(os.environ.get("AGENT_CONTEXT_WINDOW"))
    if configured:
        return configured
    direct = _positive_int(getattr(llm, "context_window", 0))
    if direct:
        return direct
    try:
        from .model_catalog import capability
        cap = capability(getattr(llm, "model", ""), getattr(llm, "_base_url", ""))
        return _positive_int(cap.context_window)
    except Exception:  # noqa: BLE001 - preflight discovery itself must not break compatibility mode
        return 0


def model_context_window(llm) -> int:
    """Public capacity lookup shared by the seed projector and strict preflight."""
    return _context_window(llm)


# Exact inverse pair (#33): _byte_upper_bound estimates bytes → tokens at this ratio, and
# tokens_to_chars converts a token budget back to a character budget with the SAME ratio, so the
# projection→estimate→check loop is algebraically consistent (a token deficit converts to exactly the
# character tightening that closes it in one pass). Change one, change both.
_TOKENS_PER_BYTE = 1.15 / 3


def tokens_to_chars(tokens: float) -> int:
    """Character-budget equivalent of a token count under ``_byte_upper_bound``'s estimate."""
    return int(tokens / _TOKENS_PER_BYTE)


def _byte_upper_bound(value: object) -> int:
    """Tokenizer-independent token estimate for text/JSON request material.

    Formerly bytes==tokens, which over-reserved ~3–4× (typical English/code runs ≈3–4 bytes per token),
    so a configured 128k window behaved like ~32–50k and fired early compaction / false local overflows
    (#33 review). Now ≈3 bytes/token with a 15% safety margin — still conservative (real ratios are
    higher), and the provider-overflow fallback below remains the ground truth when the estimate or the
    catalogued window is wrong. CJK-heavy content (≈1.5–2 bytes/token) is covered by the margin plus
    that same fallback."""
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    raw = len(body.encode("utf-8", "replace"))
    return int(raw * _TOKENS_PER_BYTE) + 1


def preflight_model_call(
    llm,
    messages: list[dict],
    tools: list[dict],
    *,
    allow_unknown: bool,
    estimator: Callable[[object], int] = _byte_upper_bound,
) -> PreflightReport:
    """Check the exact per-call messages, schemas, and configured output reserve.

    Unknown model windows remain an explicit compatibility mode during migration.  A
    configured or catalogued positive window is always enforced before provider I/O.
    """
    report = estimate_model_call(llm, messages, tools, estimator=estimator)
    window = report.context_window
    try:
        llm._last_preflight = report
    except Exception:  # noqa: BLE001 - diagnostic only
        pass
    if not window:
        if not allow_unknown:
            raise UnknownContextWindow(
                "model context window is unknown; set AGENT_CONTEXT_WINDOW or opt into compatibility mode")
        return report
    if report.required_tokens > window:
        raise PreflightOverflow(report)
    return report


def estimate_model_call(
    llm,
    messages: list[dict],
    tools: list[dict],
    *,
    estimator: Callable[[object], int] = _byte_upper_bound,
) -> PreflightReport:
    """Return the canonical physical-cost estimate without accepting/rejecting the call."""
    message_estimate = estimator(messages) + 12 * len(messages) + 16
    schema_estimate = estimator(tools) + 24 * len(tools) if tools else 0
    output_reserve = _positive_int(getattr(llm, "max_tokens", 0))
    window = _context_window(llm)
    return PreflightReport(
        estimated_input_tokens=message_estimate,
        schema_tokens=schema_estimate,
        output_reserve=output_reserve,
        context_window=window,
        mode="strict" if window else "compatibility-unknown",
    )


def available_content_capacity(llm, fixed_messages: list[dict], tools: list[dict]) -> int | None:
    """Conservative request units left for one user-content projection.

    ``fixed_messages`` should contain the system message, an empty user placeholder, and the current
    trajectory. Unknown windows return ``None`` so compatibility mode preserves the roomy projection.
    Final strict preflight still checks the exact rendered JSON and corrects escaping/Unicode overhead.
    """
    report = estimate_model_call(llm, fixed_messages, tools)
    if not report.context_window:
        return None
    # The window math above is in TOKENS; the seed projection this feeds (SeedPlan.project) budgets in
    # CHARACTERS. Convert with the exact estimator inverse so the units are algebraically consistent —
    # CJK-heavy content (fewer chars per token than ASCII) is corrected by the projection loop's exact
    # re-check + reactive provider-overflow fallback, while an underfilled seed is never corrected
    # (#33 review: the old bytes==tokens chain quarter-sized a 128k window; a token→char mismatch here
    # would preserve exactly that bug).
    return max(0, tokens_to_chars(report.context_window - report.required_tokens))


def coerce_tool_status(value: object, *, legacy_text: str | None = None) -> ToolStatus:
    """Normalize an explicit status; prose is used only by a named legacy adapter."""
    if isinstance(value, ToolStatus):
        return value
    if isinstance(value, str):
        try:
            return ToolStatus(value.lower())
        except ValueError:
            # A caller explicitly supplied lifecycle data, but it is not one of the protocol states. Treating
            # that typo/extension drift as success fabricates settlement; uncertainty is the only safe live
            # projection (recovery already applies the same rule to invalid persisted statuses).
            return ToolStatus.INDETERMINATE
    if isinstance(value, bool):
        return ToolStatus.SUCCEEDED if value else ToolStatus.FAILED
    if legacy_text is not None:
        return (ToolStatus.FAILED if legacy_text.startswith(("Error", "Exit code"))
                else ToolStatus.SUCCEEDED)
    return ToolStatus.SUCCEEDED


__all__ = [
    "CHILD_ACTIVITY_ARG", "CHILD_CANCEL_SIGNAL_ARG", "CHILD_INVOCATION_ID_ARG",
    "CHILD_REQUEST_ORDINAL_ARG", "ChildActivity", "DELEGATION_TOOL_NAMES",
    "NO_ADOPT_ON_TIMEOUT_ARG", "PreflightOverflow", "PreflightReport",
    "ToolEffect", "ToolInvocation", "ToolOutcome",
    "ToolPurity", "ToolStatus", "TurnOutcome", "TurnStatus", "UnknownContextWindow", "Usage",
    "available_content_capacity", "coerce_tool_status", "estimate_model_call", "is_delegation_tool",
    "model_context_window",
    "preflight_model_call",
]
