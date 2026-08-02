"""Core-owned values that cross the tool-registry boundary.

The coding host owns registry mutation and handler composition. The runtime owns the
descriptors, admissions, typed text, and outcome projection it consumes so core never
imports the concrete registry implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .access import AllAccess
from .execution import (ToolEffect, ToolInvocation, ToolOutcome, ToolPurity,
                        ToolStatus, coerce_tool_status)

Handler = Callable[[dict], str]
AccessFn = Callable[[dict], list]


class ToolText(str):
    """A tool result carrying explicit status, effects, and optional turn control."""

    __slots__ = ("_status", "_effects", "_control")

    def __new__(cls, value: str = "", ok: bool = True, *, status: ToolStatus | str | None = None,
                effects: tuple[ToolEffect, ...] = (), control=None):
        obj = super().__new__(cls, value)
        obj._status = coerce_tool_status(status if status is not None else ok)  # type: ignore[attr-defined]
        obj._effects = tuple(effects or ())  # type: ignore[attr-defined]
        obj._control = control  # type: ignore[attr-defined]
        return obj

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.SUCCEEDED

    @property
    def status(self) -> ToolStatus:
        return getattr(self, "_status", ToolStatus.SUCCEEDED)

    @property
    def effects(self) -> tuple[ToolEffect, ...]:
        return getattr(self, "_effects", ())

    @property
    def control(self):
        """Typed turn-control signal, never inferred from this string's text."""
        return getattr(self, "_control", None)


def _all_access(_args: dict) -> list:
    return [AllAccess()]


@dataclass
class ToolEntry:
    """Host-provided tool descriptor consumed by the runtime."""

    name: str
    schema: dict
    handler: Handler
    accesses: AccessFn = _all_access
    check: Optional[Callable[[], bool]] = None
    source: str = "builtin"
    purity: ToolPurity = ToolPurity.UNKNOWN
    deduplicable: bool = False
    turn_exclusive: bool = False
    capabilities: frozenset[str] = frozenset()
    effect_factory: Optional[
        Callable[[ToolInvocation, ToolStatus, str], tuple[ToolEffect, ...]]
    ] = None


@dataclass(frozen=True)
class ToolAdmission:
    """One-shot proof that a specific registry entry passed validation."""

    name: str
    entry: ToolEntry


def tool_result_text(value) -> str:
    """Canonical presentation coercion for handler results."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def finalize_tool_outcome(
    invocation: ToolInvocation,
    result,
    *,
    entry: ToolEntry | None = None,
    default_effect_id: str | None = None,
    park_authorized: bool = False,
) -> ToolOutcome:
    """Build the canonical typed outcome from a completed or cancelled result.

    ``park_authorized`` is supplied by the concrete registry's authority query. The
    core transport type deliberately cannot mint or inspect that private capability.
    """
    explicit = getattr(result, "status", None)
    if explicit is not None:
        status = coerce_tool_status(explicit)
    else:
        ok = getattr(result, "ok", None)
        status = (coerce_tool_status(bool(ok)) if ok is not None else
                  coerce_tool_status(None, legacy_text=tool_result_text(result)))
    text = tool_result_text(result)
    effects = tuple(getattr(result, "effects", ()) or ())
    factory = getattr(entry, "effect_factory", None)
    if factory is not None:
        try:
            effects = tuple(factory(invocation, status, text) or ())
        except (Exception, SystemExit) as error:
            status = ToolStatus.INDETERMINATE
            text = f"Error: tool effect construction failed ({type(error).__name__}: {error})"
            effects = ()
    if not effects:
        effect_id = default_effect_id or f"invoke:{invocation.provider_index}:{invocation.id}:0"
        effects = (ToolEffect(
            effect_id, "tool_outcome", {"name": invocation.name, "status": status.value},
        ),)

    from .interfaces import PeerParkControl
    control = result if isinstance(result, PeerParkControl) else getattr(result, "control", None)
    if control is not None and not isinstance(control, PeerParkControl):
        control = None
    if control is not None and not park_authorized:
        control = None
    if control is not None and status is not ToolStatus.SUCCEEDED:
        control = None
    return ToolOutcome(
        invocation=invocation, status=status, text=text, effects=effects, control=control,
    )


__all__ = [
    "AccessFn",
    "Handler",
    "ToolAdmission",
    "ToolEntry",
    "ToolText",
    "finalize_tool_outcome",
    "tool_result_text",
]
