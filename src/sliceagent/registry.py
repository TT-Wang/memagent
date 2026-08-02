"""ToolRegistry — one registry, many sources (builtin / MCP / plugin / skill).

A `generation` counter plus a `check` availability gate project the three sources into
one registry. The ToolHost projects schemas()/run()/accesses() from here, so every
tool — wherever it comes from — satisfies one contract and appears in one list. This is
the keystone of Step ③: MCP, plugins, and skills all register into the SAME registry the
loop already drives.
"""
from __future__ import annotations

from sliceagent_core.access import AllAccess
from sliceagent_core.execution import (ToolInvocation, ToolOutcome, ToolPurity,
                                       ToolStatus)
from sliceagent_core.registry_types import (
    ToolAdmission,
    ToolEntry,
    ToolText,
    finalize_tool_outcome,
    tool_result_text,
)

from .reach import ReachSteer


def _missing_required(schema: dict, args: dict) -> list:
    """Required parameters the tool schema declares that are absent (or None) in the call. Present-but-
    empty (e.g. content="") counts as supplied; only truly-missing args are flagged."""
    params = schema["function"].get("parameters", {})
    a = args if isinstance(args, dict) else {}
    return [r for r in (params.get("required") or []) if a.get(r) is None]


def _validate_entry_schema(entry: "ToolEntry") -> None:
    """Reject malformed extension schemas before they can enter the shared registry."""
    if not isinstance(entry.name, str) or not entry.name.strip():
        raise ValueError("tool name must be a non-empty string")
    for field_name in ("handler", "accesses"):
        if not callable(getattr(entry, field_name, None)):
            raise ValueError(f"tool {entry.name!r} {field_name} must be callable")
    for field_name in ("check", "effect_factory"):
        value = getattr(entry, field_name, None)
        if value is not None and not callable(value):
            raise ValueError(f"tool {entry.name!r} {field_name} must be callable when provided")
    schema = entry.schema
    if not isinstance(schema, dict):
        raise ValueError(f"tool {entry.name!r} schema must be a mapping")
    function = schema.get("function")
    if not isinstance(function, dict):
        raise ValueError(f"tool {entry.name!r} schema.function must be a mapping")
    declared = function.get("name")
    if not isinstance(declared, str) or declared != entry.name:
        raise ValueError(f"tool {entry.name!r} schema name must match the registry name")
    parameters = function.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError(f"tool {entry.name!r} schema parameters must be a mapping")
    properties = parameters.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError(f"tool {entry.name!r} schema properties must be a mapping")
    required = parameters.get("required", [])
    if not isinstance(required, (list, tuple)) or not all(isinstance(item, str) for item in required):
        raise ValueError(f"tool {entry.name!r} schema required must be a list of strings")


# Park authority is a REGISTRATION-PATH fact, never entry data. `turn_exclusive` is
# caller-supplied: a plugin/MCP descriptor can set it, and self-assertion is not authority.
# Only the host-held TurnControlRegistrar port stamps this module-private sentinel, and the
# ordinary register() path strips it. Minting is NOT on ToolRegistry: that object is shared
# with plugin/MCP/skill registration, so reaching it must not be enough to grant the capability.
_PARK_AUTHORITY = object()
_PARK_STAMP = "_park_authority"


def park_authorized(entry: object) -> bool:
    """True only for an entry granted authority through the host-held TurnControlRegistrar."""
    return getattr(entry, _PARK_STAMP, None) is _PARK_AUTHORITY


# Compatibility metadata for built-ins registered before ToolEntry carried execution
# properties. New/plugin/MCP tools remain UNKNOWN unless they declare otherwise.
_PURE_READ_BUILTINS = frozenset({
    "read_file", "list_files", "grep", "glob", "search_history", "code_review",
    # GET-only web reads: pure reads (no side effects), so the generic tool deadline and read waves
    # apply — classifying them EFFECTFUL made AGENT_TOOL_TIMEOUT silently never fire for them.
    "fetch_url", "web_search",
})
_DEDUPLICABLE_BUILTINS = frozenset({"read_file", "list_files", "grep", "glob", "search_history"})

class ToolRegistry:
    """A name->ToolEntry map with a generation counter (for downstream schema caching)
    and a per-tool availability gate. Robust by construction: a flaky check or handler
    hides/erros the one tool, never the whole registry."""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}
        self.generation = 0

    def register(self, entry: ToolEntry, *, override: bool = False) -> None:
        # Capability removal, not detection: the ordinary path can never carry park authority,
        # so a descriptor that arrives pre-stamped (a plugin guessing the attribute, a recycled
        # entry object) loses it here rather than being screened for later.
        try:
            entry.__dict__.pop(_PARK_STAMP, None)
        except AttributeError:
            pass
        _validate_entry_schema(entry)
        if getattr(entry, "turn_exclusive", False):
            # Contradictory metadata, rejected at the boundary rather than handled downstream.
            # A turn-ending control call SUSPENDS the turn; it is neither a pure read nor
            # replayable from a sibling's result. Allowing the combination let a deduplicated
            # twin take a compatibility path that bypassed the frozen audit projection and
            # published the raw subject.
            if entry.effect_factory is not None:
                raise ValueError(
                    f"tool {entry.name!r} is turn_exclusive and cannot declare a custom "
                    "effect_factory: a control call's effects would carry model-authored "
                    "content into durable audit"
                )
            if entry.deduplicable or entry.purity is ToolPurity.PURE_READ:
                raise ValueError(
                    f"tool {entry.name!r} is turn_exclusive and cannot be deduplicable "
                    "or PURE_READ: a turn-ending control call is neither replayable nor a read"
                )
        if entry.name in self._tools and not override:
            raise ValueError(f"tool {entry.name!r} already registered (pass override=True to replace)")
        if entry.source == "builtin" and entry.purity is ToolPurity.UNKNOWN:
            entry.purity = (ToolPurity.PURE_READ if entry.name in _PURE_READ_BUILTINS
                            else ToolPurity.EFFECTFUL)
        if entry.source == "builtin" and entry.name in _DEDUPLICABLE_BUILTINS:
            entry.deduplicable = True
        self._tools[entry.name] = entry
        self.generation += 1

    def deregister(self, name: str) -> None:
        if self._tools.pop(name, None) is not None:
            self.generation += 1

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return [e.name for e in self._available()]

    def entry(self, name: str) -> ToolEntry | None:
        """Canonical metadata lookup. Unknown tools stay conservative in callers."""
        return self._tools.get(name)

    def park_authorized(self, entry: ToolEntry | None) -> bool:
        """Project the private registration-path authority through the core port."""
        return park_authorized(entry)

    def _available(self) -> list[ToolEntry]:
        out = []
        for e in self._tools.values():
            try:
                if e.check is None or e.check():
                    out.append(e)
            except (Exception, SystemExit):
                pass  # a flaky availability check hides that tool, never crashes the registry
        return out

    def schemas(self) -> list[dict]:
        return [e.schema for e in self._available()]

    def accesses(self, name: str, args: dict) -> list:
        e = self._tools.get(name)
        if e is None:
            return [AllAccess()]
        try:
            return e.accesses(args)
        except (Exception, SystemExit):
            return [AllAccess()]

    def admit(self, name: str, args: dict) -> tuple[ToolAdmission | None, ToolText | None]:
        """Validate once and return either an executable admission or a conclusive failure."""
        entry = self._tools.get(name)
        if entry is None:
            return None, ToolText(f'Error: unknown tool "{name}"', ok=False)
        if entry.check is not None:
            try:
                available = bool(entry.check())
            except (Exception, SystemExit):
                available = False
            if not available:
                return None, ToolText(f'Error: tool "{name}" is currently unavailable', ok=False)
        missing = _missing_required(entry.schema, args)
        if missing:
            return None, ToolText(
                f'Error: {name} missing required argument(s): {", ".join(missing)}', ok=False,
            )
        return ToolAdmission(name, entry), None

    def preflight(self, name: str, args: dict) -> ToolText | None:
        """Return a conclusive pre-handler failure, or ``None`` when execution may start.

        The ordered loop calls this before publishing ToolExecutionStarted. ``run`` repeats it for direct
        callers and for availability gates that can change between admission and handler entry.
        """
        _, failure = self.admit(name, args)
        return failure

    @staticmethod
    def _run_admitted(admission: ToolAdmission, args: dict) -> ToolText:
        """Enter an already-admitted handler without repeating its volatile availability check."""
        e = admission.entry
        try:
            out = e.handler(args)
            if isinstance(out, ToolText):
                return out
            from .interfaces import PeerParkControl as _PPC
            if isinstance(out, _PPC) and park_authorized(e):
                # A park is CONTROL, not output. Converting it with tool_result_text would both
                # lose the typed signal (production would silently never park) and stringify the
                # correlation into the model-visible transcript. Body-free text, typed control.
                return ToolText("Waiting on the collaborator.", ok=True, control=out)
            if isinstance(out, _PPC):
                # Minting a park is HOST authority, granted only by TurnControlRegistrar. A plugin/MCP or
                # any undeclared tool returning a carrier is a protocol error, not a park: it
                # would let unauthorized code suspend the turn. Fail loudly rather than drop it
                # silently, so a miswired host is visible instead of mysteriously never parking.
                return ToolText(
                    "Error: this tool is not authorized to end the turn on a peer wait",
                    ok=False,
                )
            return ToolText(tool_result_text(out), ok=True)
        except ReachSteer as ex:
            # Only the built-in resolver can prove this exception happened before
            # an effect. Extensions retain the conservative failure/uncertainty rule.
            if e.source == "builtin":
                return ToolText(str(ex), status=ToolStatus.STEERED)
            uncertain_extension = e.purity is not ToolPurity.PURE_READ
            status = ToolStatus.INDETERMINATE if uncertain_extension else ToolStatus.FAILED
            suffix = (" (the extension may have applied side effects before raising)"
                      if uncertain_extension else "")
            return ToolText(f"Error: {ex}{suffix}", status=status)
        except (Exception, SystemExit) as ex:
            uncertain_extension = (e.source != "builtin" and e.purity is not ToolPurity.PURE_READ)
            status = ToolStatus.INDETERMINATE if uncertain_extension else ToolStatus.FAILED
            suffix = (" (the extension may have applied side effects before raising)"
                      if uncertain_extension else "")
            return ToolText(f"Error: {ex}{suffix}", status=status)

    def run_admitted(self, admission: ToolAdmission, args: dict) -> ToolText:
        """Execute a token returned by :meth:`admit`; intended for ordered host/scheduler integration."""
        if not isinstance(admission, ToolAdmission):
            return ToolText("Error: invalid tool admission", ok=False)
        return self._run_admitted(admission, args)

    def run(self, name: str, args: dict) -> ToolText:
        """The single tool choke point. Returns ToolText (a str carrying .ok) so the loop reads an
        EXPLICIT success flag rather than re-inferring failure from prose. ok=False means a non-success:
        an unknown tool, a raised handler (FAILED or INDETERMINATE by contract), or a handler that returned
        ToolText(ok=False) itself (e.g. a nonzero exit code, a not-unique str_replace). A handler that returns
        a plain string is SUCCESS —
        even if that string happens to begin with "Error" (a grep hit, a log line).

        An extension handler may mutate before it raises. For UNKNOWN/EFFECTFUL plugin, MCP, or skill
        entries, a raised exception therefore means INDETERMINATE rather than FAILED; the ordered scheduler
        cancels later operations in that provider batch so they cannot overtake an unknown effect. The receipt
        then carries that uncertainty as advisory evidence. A declared PURE_READ extension has no side effects
        to leave unresolved, so its exception remains a normal failure.
        """
        admission, failure = self.admit(name, args)
        if failure is not None:
            return failure
        return self._run_admitted(admission, args)

    def invoke(self, invocation: ToolInvocation, *, call_args: dict | None = None,
               default_effect_id: str | None = None) -> ToolOutcome:
        """Execute through the registry, then use the canonical typed-outcome boundary.

        ``invocation.args`` remains the raw provider/audit record supplied to effect factories; ``call_args``
        optionally supplies the sanitized handler view. Production wrappers execute themselves and call the
        same :func:`finalize_tool_outcome` helper so wrapper-level restrictions are never bypassed.
        """
        args = dict(invocation.args) if call_args is None else dict(call_args)
        admission, failure = self.admit(invocation.name, args)
        if failure is not None:
            # No handler boundary was crossed, so an execution-only effect factory must not run.
            return finalize_tool_outcome(
                invocation, failure, entry=None, default_effect_id=default_effect_id,
            )
        out = self._run_admitted(admission, args)
        return finalize_tool_outcome(
            invocation, out, entry=admission.entry,
            default_effect_id=default_effect_id,
            park_authorized=self.park_authorized(admission.entry),
        )


class TurnControlRegistrar:
    """The host-held port that GRANTS park authority. Nothing else mints it.

    Minting deliberately does not live on ToolRegistry: the registry is shared with plugin,
    MCP and skill registration paths, so holding the registry must not be sufficient to grant
    a turn-ending capability. The registry may still strip and validate; only this port grants.

    The host constructs one at wiring time and keeps it. In-process code can of course import
    this class — plugins run in the same interpreter — so the boundary this enforces is
    "reaching the shared registry is not enough", not sandboxing hostile in-process code.
    """

    __slots__ = ("_registry",)

    def __init__(self, registry: "ToolRegistry"):
        self._registry = registry

    def register(self, entry: ToolEntry, *, override: bool = False) -> None:
        if entry.source not in ("builtin", "host"):
            # Defense in depth for a miswired host: an untrusted descriptor routed into the
            # authority port fails loudly at registration rather than at park time.
            raise ValueError(
                f"tool {entry.name!r} from source {entry.source!r} cannot be registered as a "
                "turn-control tool: only host-owned tools may end the turn on a peer wait"
            )
        if not entry.turn_exclusive:
            # The scheduling flag and the authority must agree, or the loop would grant a park
            # to a call that was never isolated in its batch.
            raise ValueError(
                f"tool {entry.name!r} must set turn_exclusive to be registered as turn-control"
            )
        self._registry.register(entry, override=override)
        self._registry._tools[entry.name].__dict__[_PARK_STAMP] = _PARK_AUTHORITY
