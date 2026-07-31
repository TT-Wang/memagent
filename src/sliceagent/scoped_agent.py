"""SCOPED AGENT — a delegation as a *scoped turn*, not a nested agent.

Thesis (docs/SUBAGENT-SCOPED-TURN.md): a subagent is not a new kind of object — it is the same turn
morphism over the sealed archive, with a restricted domain. Where the nested-subagent stack builds a
child host, a separate child-seed path, an observation sink, a seal contract, private state, roster,
depth/grants — this reproduces a delegation with three ingredients that already exist:

  1. a fresh Slice seeded with the sub-task AS ACTIVE WORK (so the dependency-first compiler scopes the
     context — furniture like the repo map is shed automatically: the R5 baggage gate keys on active_work);
  2. a ScopedSurface — the plan-mode read-only pattern, gating EVERY dispatch protocol (run +
     preflight_run/run_preflighted; the plan-mode review proved that filtering only run() is bypassed);
  3. run_turn — the same loop the parent uses. Its report is the last assistant text.

Isolation is free (the scoped turn's accumulated context is discarded; only the report survives).
model_id is threaded so the child shares the parent's cache prefix (the R6 fix, for free — one seed path).
Depth 1 is free: the spawn tool is simply absent from the child's allowed surface.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field

import posixpath

from .agents import READ_ONLY_TOOLS, SUBAGENT_EXCLUDED_TOOLS
from .context import ResourceKind, ResourceRef, reserved_resource_ref
from .events import AssistantText, StepEnd, ToolResult
from .execution import ToolStatus
from .hooks import Hooks
from .loop import run_turn
from .pfc import Slice, record_user
from .registry import ToolText
from .safety import redact_text
from .seed import make_build_slice

# Depth 1 by construction: no child surface ever contains a spawn tool.
SPAWN_TOOLS = frozenset({"spawn_agent", "spawn_explore", "spawn_subagent"})

# Tools no child may ever call, regardless of kind: delegation (depth 1), the end-user channel
# (ambiguity is the parent's to resolve), the parent's Active Work, workspace rerooting, and the
# parent's private history index. Hallucinated calls are harmless request-shape corrections (↷).
CHILD_STEERED_TOOLS = SPAWN_TOOLS | SUBAGENT_EXCLUDED_TOOLS | {"search_history"}

# Parent-private resource kinds: a child's report is built from the WORKSPACE, never from the
# parent's archives (its history, other children's seals, artifact provenance).
_CHILD_PRIVATE_RESOURCE_KINDS = frozenset({
    ResourceKind.ARTIFACT, ResourceKind.HISTORY, ResourceKind.SUBAGENT, ResourceKind.ROSTER,
    ResourceKind.INTERNAL_CONTEXT,
})
_READ_TOOLS = frozenset({"read_file", "list_files", "grep", "glob"})


def _norm_vpath(path) -> str:
    """CANONICAL virtual-namespace path ('./subagents\\sub-1.md/' -> 'subagents/sub-1.md'). posixpath.normpath
    collapses '..' and '.' SEGMENTS — load-bearing for every prefix-based guard downstream: without it,
    a '../'-spelled path passes a prefix check and the mounted FS then normalizes it into another
    namespace (guard and FS must normalize identically, or the gap between them is a traversal)."""
    p = (path or "").strip().replace("\\", "/") if isinstance(path, str) else ""
    if not p:
        return ""
    p = posixpath.normpath(p)
    return "" if p == "." else p.rstrip("/")


def _classified_read_target(args, resource_ref=None, *, canonicalize=None):
    """Return the host-routed resource, its canonical handle, and private-host-dir status.

    Namespace spelling alone is not authoritative: ``/workspace/history/turn-1.md`` can be the same
    virtual handle as ``history/turn-1.md``, while a real ``history/`` or ``artifacts/`` project path
    shadows that mount and must remain readable. Prefer the host's canonical ``resource_ref`` seam;
    the lexical fallback exists only for minimal legacy/test hosts."""
    path = args.get("path") if isinstance(args, dict) else ""
    ref = None
    if callable(resource_ref):
        try:
            candidate = resource_ref(str(path or ""))
            if isinstance(candidate, ResourceRef):
                ref = candidate
        except Exception:  # noqa: BLE001 — a classifier failure must fail closed via the lexical fallback
            ref = None
    if ref is None:
        ref = reserved_resource_ref(_norm_vpath(path))
    canonical_source = ref.handle if ref.virtual else path
    if not ref.virtual and callable(canonicalize):
        try:
            canonical_source = canonicalize(str(path or ""))
        except Exception:  # noqa: BLE001 — lexical fallback still protects the ordinary relative spelling
            pass
    canonical = _norm_vpath(canonical_source)
    # `.sliceagent/` is a physical host-private store, not a virtual archive kind. Keep the existing
    # default-deny for both its relative spelling and the absolute spelling canonicalized by the host.
    private = canonical == ".sliceagent" or canonical.startswith(".sliceagent/")
    return ref, canonical, private


def allowed_for(spec, inner) -> tuple[str, ...]:
    """A child's tool allowlist: the kind's own list, or (tools=None → 'general') the inner host's
    full surface. Child-barred tools are excluded either way — depth 1 and parent-privacy by
    construction, not by schema courtesy."""
    if spec.tools is not None:
        names = spec.tools
    else:
        names = tuple(s.get("function", {}).get("name", "") for s in inner.schemas())
    return tuple(n for n in names if n and n not in CHILD_STEERED_TOOLS)


def _private_child_read(name: str, args, inner) -> bool:
    """True when a child read ROUTES to a parent-private resource (any spelling). Routing-aware, not
    lexical: a real project ``artifacts/`` path that shadows the mount stays readable."""
    if name not in _READ_TOOLS or not isinstance(args, dict):
        return False
    ref, _, private_path = _classified_read_target(
        args, getattr(inner, "resource_ref", None),
        canonicalize=getattr(inner, "_archive_handle", None),
    )
    return ref.kind in _CHILD_PRIVATE_RESOURCE_KINDS or private_path

# Spec §5 acceptance vocabulary. Evidence is an informational label, never a gate.
# ``indeterminate`` stays DISTINCT from ``failed``: an unconfirmed-close/timeout child has an unknown
# physical state — collapsing it into "failed" would erase truth the UI and parent can act on.
# This table holds only reasons whose status does NOT depend on what the child delivered. Provider
# finish reasons like ``max_tokens``/``filtered`` deliberately stay OUT of it: whether being cut off
# counts as partial work or as delivering nothing is exactly the content question below.
_STOP_TO_STATUS = {
    "aborted": "cancelled",
    "max_steps": "partial", "token_budget": "partial", "overflow": "partial",
    "indeterminate": "indeterminate",
}


def classify_outcome(stop_reason: str, report: str) -> str:
    """Map one scoped turn's stop reason to the acceptance vocabulary.

    THE INVARIANT: an outcome that CARRIES A REPORT is never ``failed``. A child that wrote real
    findings and then hit a ceiling — the provider's completion cap, a content filter, or a stop
    reason this table has never seen — delivered partial work. Calling that "failed" throws the work
    away and invites a pointless re-run, and it silently mis-fires on every stop reason added
    upstream in future (that is exactly how a truncated 5.7k-char review got reported as a failure).
    Only a genuinely empty outcome fails.
    """
    if stop_reason == "end_turn":
        return "ok" if report else "failed"
    mapped = _STOP_TO_STATUS.get(stop_reason)
    if mapped is not None:
        return mapped
    return "partial" if report else "failed"


class ScopedSurface:
    """A tool host restricted to ``allowed`` tool names for one scoped turn.

    Three gate tiers, carried from the proven child-surface taxonomy:
      * child-barred tools (spawn/ask_user/update_work/…) → quiet steer (↷ CANCELLED — a hallucinated
        call is a request-shape correction, not a failure);
      * a tool OUTSIDE the allowlist but real on the inner host → LOUD failure. Schema hiding is not a
        security boundary: a read-only child emitting a write call is a capability-escalation attempt;
      * parent-private mounts (artifacts/, history/, subagents/, @sliceagent/) → quiet steer on reads —
        a child works from the WORKSPACE, never the parent's archives.
    Every dispatch protocol run_tool_batch may use is gated (run + preflight_run/run_preflighted; the
    plan-mode review proved that filtering only run() is bypassed). All other attributes delegate
    unchanged, so permitted reads behave exactly as a normal turn.
    """

    def __init__(self, inner, allowed):
        self._inner = inner
        self._allowed = frozenset(allowed)
        # Parent attachments belong to the parent's next provider call. Delegated children share the host
        # surface for workspace reads, but must not race to consume or leak that private one-shot payload.
        self.pending_images = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _gate(self, name, args=None):
        if name in CHILD_STEERED_TOOLS:
            if name == "ask_user":
                return ToolText(
                    "a subagent cannot ask the user. Decide on a reasonable assumption, proceed, and "
                    "state the assumption in your summary; the parent will handle any real ambiguity.",
                    status=ToolStatus.CANCELLED)
            return ToolText(
                f"a subagent cannot call {name!r}; keep working within the delegated task and report "
                "any needed parent action in the final summary.", status=ToolStatus.CANCELLED)
        if name not in self._allowed:
            return ToolText(f"Error: tool {name!r} is not available to this agent",
                            status=ToolStatus.FAILED)
        if _private_child_read(name, args, self._inner):
            return ToolText(
                "artifacts/, history/, subagents/, roster/ and @sliceagent/ are the parent's private "
                "namespace; work from the workspace files in your task scope and report what you need "
                "in your summary.", status=ToolStatus.CANCELLED)
        return None

    def schemas(self):
        return [s for s in self._inner.schemas()
                if s.get("function", {}).get("name") in self._allowed]

    def run(self, name, args):
        return self._gate(name, args) or self._inner.run(name, args)

    def preflight_run(self, name, args):
        steer = self._gate(name, args)
        if steer is not None:
            return None, steer
        preflight = getattr(self._inner, "preflight_run", None)
        run_preflighted = getattr(self._inner, "run_preflighted", None)
        if callable(preflight) != callable(run_preflighted):
            # Preserve the kernel's incomplete-protocol rejection — never paper over half a protocol.
            return None, ToolText(
                "Error: wrapped tool host exposes an incomplete one-shot preflight protocol",
                status=ToolStatus.FAILED)
        return preflight(name, args) if callable(preflight) else (None, None)

    def run_preflighted(self, name, args, admission):
        steer = self._gate(name, args)
        if steer is not None:
            return steer
        inner = getattr(self._inner, "run_preflighted", None)
        return inner(name, args, admission) if callable(inner) else self._inner.run(name, args)


def scoped_llm_view(llm, reasoning: str = "", transport_activity=None):
    """The llm VIEW for a scoped child: a SHALLOW COPY (shares the thread-safe transport + gate),
    never the parent object. The parent's streaming delta/activity sinks are DISCONNECTED so
    concurrent children never write into the parent's renderer, and a child's model/_fellback
    mutation on overflow stays child-local (the S7 lesson, carried from the nested stack)."""
    view = copy.copy(llm)
    if reasoning:
        view.reasoning = reasoning
    if hasattr(view, "set_delta_sink"):
        view.set_delta_sink(None)
    else:
        view._on_delta = None
    if hasattr(view, "set_transport_activity"):
        view.set_transport_activity(transport_activity)
    else:
        view._transport_activity = transport_activity
    return view


@dataclass
class ScopedResult:
    """One scoped turn's outcome: the redacted report plus honest accounting for the parent."""

    report: str = ""
    status: str = "failed"          # ok | partial | failed | cancelled | indeterminate  (spec §5)
    stop_reason: str = ""
    steps: int = 0
    elapsed: float = 0.0
    usage: dict = field(default_factory=dict)   # summed over StepEnd: prompt/completion/cache splits

    @property
    def report_completion(self) -> str:
        """Report-byte completeness, INDEPENDENT of execution state (tui_projection vocabulary).

        A child can finish cleanly and say nothing (``absent``), or be cut off mid-sentence with
        real findings (``partial``). The parent's coverage count keys on this, not on status.
        """
        if not self.report:
            return "absent"
        return "partial" if self.status in ("partial", "indeterminate") else "complete"

    @property
    def explorer_evidence_status(self) -> str:
        """The typed evidence label for the TUI matrix (tui_projection vocabulary).

        The report body travels INLINE in the delegation ToolResult, so at settle time the
        parent's context already holds it: a complete report is ``content_retained``, a cut-off
        one ``content_partial``, and no report means there was nothing to retain (``none``).
        The label is informational — never an acceptance gate.
        """
        return {
            "complete": "content_retained",
            "partial": "content_partial",
        }.get(self.report_completion, "none")

    def to_record(self) -> dict:
        """THE canonical projection of a child outcome — the single source every surface derives
        from: the parent's typed effects, the durable seal, the recall view, and the TUI matrix.

        This exists because hand-writing one projection per surface is how three capabilities went
        dark at once: ``stop_reason`` never reached the recall view (so an agent had to GUESS why a
        child stopped), ``report_completion`` never reached the parent (so the matrix reported
        "0/6 reports ready" while six reports existed), and ``model_usage`` never reached the loop
        (so child tokens vanished from the turn budget). Add a field here and it lands everywhere;
        add it to one call site and it silently lands nowhere else.
        """
        return {
            "status": self.status,
            "operational_status": self.status,
            "stop_reason": self.stop_reason,
            "stop_cause": self.stop_reason,
            "steps": self.steps,
            "elapsed_s": round(self.elapsed, 3),
            "report": self.report,
            "report_bytes": len(self.report.encode("utf-8")),
            "report_completion": self.report_completion,
            "explorer_evidence_status": self.explorer_evidence_status,
            "partial": self.status in ("partial", "indeterminate"),
            "usage": dict(self.usage or {}),
        }


def run_scoped_agent(task: str, *, tools, llm, retriever, memory, allowed_tools=READ_ONLY_TOOLS,
                     model_id: str = "", max_steps: int = 100, signal=None,
                     reasoning: str = "", system_extra: str = "", on_event=None,
                     transport_activity=None) -> ScopedResult:
    """Run one scoped turn and return a ScopedResult.

    The report is the child's last assistant text (mirrors the explorer's summary-is-deliverable). A
    ToolResult resets it, so a preamble before a tool call cannot masquerade as the final report; the
    park-closeout's best-effort summary therefore survives as the report on partial stops. ``on_event``
    (optional) receives the child's raw loop events — enough for a caller to project progress phases.
    """
    state = Slice()
    state.reset(task)
    record_user(state, task, source_event_id="scoped-1", logical_id="scoped-1")
    surface = ScopedSurface(tools, allowed_tools)
    child_llm = scoped_llm_view(llm, reasoning, transport_activity=transport_activity)
    build = make_build_slice(state, surface, retriever, memory, task, system_extra=system_extra,
                             model_id=model_id or getattr(child_llm, "model", ""))

    report = {"text": ""}
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "input_cache_read": 0,
             "input_cache_creation": 0, "input_other": 0}

    def dispatch(event):
        if isinstance(event, ToolResult):
            report["text"] = ""
        elif isinstance(event, AssistantText) and event.content:
            report["text"] = event.content
        elif isinstance(event, StepEnd):
            for key in usage:
                usage[key] += int((event.usage or {}).get(key) or 0)
        if on_event is not None:
            try:
                on_event(event)
            except Exception:  # noqa: BLE001 — a presentation observer must never kill the child
                pass

    started = time.monotonic()
    result = run_turn(build_slice=build, llm=child_llm, tools=surface, dispatch=dispatch,
                      hooks=Hooks(), max_steps=max_steps, signal=signal)
    text = redact_text((report["text"] or "").strip())
    status = classify_outcome(result.stop_reason, text)
    return ScopedResult(report=text, status=status, stop_reason=result.stop_reason,
                        steps=result.steps, elapsed=time.monotonic() - started, usage=usage)
