"""SCOPED SPAWN HOST — `spawn_agent` as a scoped turn (docs/SUBAGENT-SCOPED-TURN.md).

Replaces SubagentHost. The tool schema is UNCHANGED (agent/task/work_item_id/scope/exclusions); what
changed is everything behind it: a delegation is now one ``run_scoped_agent`` call — the ordinary
``run_turn`` over a fresh scoped slice — instead of a nested-agent stack (observation sink, seal
contract, admission preflight, roster, grants).

What the LOOP already provides (this host adds none of it):
  * cancellation — run_tool_batch injects a ``_ChildCancellationLease`` (parent Esc composed) into the
    call args; the scheduler cancels via ``request_cancel=lease.request``; the lease is Event-like and
    plugs straight into ``run_scoped_agent(signal=...)``;
  * delegation liveness — the execution protocol classifies ``spawn_agent`` as a delegation tool, so the
    scheduler's per-child inactivity window marks a silent child INDETERMINATE while active siblings
    continue, with an absolute leak guard as the final backstop;
  * parallelism — this host declares ``accesses``: a read-only kind advertises ReadAllAccess (children
    overlap in the wave), a writable kind advertises AllAccess (globally exclusive barrier).

The durable seal is DUMB: one redacted JSON record via ``memory.append_subagent_artifact`` — the
existing subagents/sub-N.md reader, FTS mirror, and recall surface consume it unchanged.
"""
from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping

from .events import (AssistantText, ModelCallPrepared, StepBegin, StepEnd, SubagentProgress,
                     ToolStarted)
from .access import AllAccess, ReadAllAccess
from .background import background_absolute_timeout
from .execution import (CHILD_ACTIVITY_ARG, CHILD_CANCEL_SIGNAL_ARG, CHILD_INVOCATION_ID_ARG,
                        CHILD_REQUEST_ORDINAL_ARG, ToolEffect, ToolStatus, Usage)
from .interfaces import PeerMessage
from .registry import ToolText
from .scoped_agent import allowed_for, run_scoped_agent

_SCOPE_PARAM = {
    "type": "array", "items": {"type": "string"},
    "description": ("optional exact areas/files/questions in scope; for broad reviews pass a "
                    "source-weight-bounded path set rather than a whole repository or one child "
                    "per directory"),
}
_EXCLUSIONS_PARAM = {
    "type": "array", "items": {"type": "string"},
    "description": "optional explicit exclusions; the child reports rather than crossing them",
}


def _primary_arg(args) -> str:
    """The one informative arg for a compact activity line (path/command/pattern/…), whitespace-collapsed."""
    if not isinstance(args, dict):
        return ""
    for k in ("path", "command", "pattern", "name", "ref", "goal", "task"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:50]
    return ""


def _steered(message: str) -> ToolText:
    text = str(message)
    if text.startswith("Error: "):
        text = text[len("Error: "):]
    return ToolText(text, status=ToolStatus.STEERED)


def _standing_constraints(intent_state) -> tuple[str, ...]:
    """Forward only still-binding standing constraints, never the parent's current request.

    The current user request is the PARENT's orchestration context, never a binding child constraint:
    replicating it into every scoped child tells each explorer to perform the parent fan-out and conflicts
    with its one-task objective. Keep only independent standing constraints, verbatim and without paraphrase.
    """
    if intent_state is None:
        return ()
    request = str(getattr(intent_state, "current_request", "") or "")
    if hasattr(intent_state, "resident_entries"):
        entries = intent_state.resident_entries()
    elif hasattr(intent_state, "open_entries"):
        entries = intent_state.open_entries()
    elif hasattr(intent_state, "entries"):
        entries = getattr(intent_state, "entries")
    else:
        entries = intent_state
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Iterable):
        return ()
    out = []
    for entry in entries:
        if isinstance(entry, Mapping):
            text = entry.get("verbatim_clause")
            status = entry.get("status", "active")
        else:
            text = getattr(entry, "verbatim_clause", None)
            status = getattr(entry, "status", "active")
        if not isinstance(text, str) or not text.strip() or text == request:
            continue
        if status not in (None, "active", "provisionally_satisfied"):
            continue
        out.append(text)
    return tuple(dict.fromkeys(out))


class _ProgressEmitter:
    """Project a child's raw loop events into typed SubagentProgress for the TUI matrix.

    Stable identities + a monotonic sequence, same contract the matrix already enforces. Phases are
    execution facts from the child's own dispatch: starting → awaiting_model → writing/running_tool
    (cycling) → settling; the terminal state is the outer ToolResult, not a phase."""

    def __init__(self, notify, *, activity=None, **identity):
        self._notify = notify
        self._activity = activity
        self._identity = identity
        self._lock = threading.Lock()
        self._seq = 0
        self._tools = 0
        self._last = ("", "")

    def __call__(self, event) -> None:
        self._touch()
        if isinstance(event, StepBegin):
            self._publish("starting", f"pass {event.step}")
        elif isinstance(event, ModelCallPrepared):
            self._publish("awaiting_model", "")
        elif isinstance(event, AssistantText):
            if getattr(event, "final", True) and not getattr(event, "synthetic", False):
                self._publish("writing", "")
        elif isinstance(event, ToolStarted):
            with self._lock:
                self._tools += 1
            name = getattr(event, "name", "") or ""
            self._publish("running_tool", f"{name} {_primary_arg(getattr(event, 'args', None))}".rstrip(),
                          tool_name=name)
        elif isinstance(event, StepEnd):
            self._publish("awaiting_model", "")

    def settling(self) -> None:
        self._touch()
        self._publish("settling", "")

    def announce(self, phase: str, detail: str) -> None:
        """Synchronous row creation BEFORE a detached spawn returns: once the spawn ToolResult
        settles, the call is no longer an active agent tool and the matrix would reject a
        first-time row arriving from the child thread."""
        self._publish(phase, detail)

    def terminal(self, phase: str, detail: str, *, evidence_status: str = "",
                 report_completion: str = "") -> None:
        """Out-of-band terminal settle for a DETACHED child: the typed label vocabulary the
        in-band ToolResult would have carried, published on the same identity/sequence channel."""
        self._touch()
        with self._lock:
            self._seq += 1
            seq, tools = self._seq, self._tools
        if self._notify is None:
            return
        try:
            self._notify(SubagentProgress(phase=phase, detail=detail, sequence=seq,
                                          tool_count=tools, evidence_status=evidence_status,
                                          report_completion=report_completion, **self._identity))
        except Exception:  # noqa: BLE001 — presentation must never affect the child
            pass

    def _touch(self) -> None:
        if self._activity is not None:
            self._activity.touch()

    def _publish(self, phase: str, detail: str, tool_name: str = "") -> None:
        with self._lock:
            if (phase, detail) == self._last:
                return
            self._last = (phase, detail)
            self._seq += 1
            seq, tools = self._seq, self._tools
        if self._notify is None:
            return
        try:
            self._notify(SubagentProgress(phase=phase, detail=detail, tool_name=tool_name,
                                          sequence=seq, tool_count=tools, **self._identity))
        except Exception:  # noqa: BLE001 — presentation must never affect the child
            pass


class ScopedSpawnHost:
    """ToolHost wrapper adding the ONE delegation tool, `spawn_agent`, over the scoped-turn core.

    Every real tool delegates to the wrapped host; children receive the RAW inner host (never this
    wrapper), so depth 1 holds by construction. `agents` maps kind → AgentSpec (builtins + agents/*.md).
    """

    def __init__(self, inner, *, llm, retriever, memory, agents=None, notify=None,
                 session_id: str = "", max_steps: int = 100, intent_provider=None, turn_id_fn=None,
                 work_provider=None, model_id: str = "", background=None):
        from .agents import BUILTIN_AGENTS
        self._inner = inner
        self._llm = llm
        self._retriever = retriever
        self._memory = memory
        self.agents = dict(agents if agents is not None else BUILTIN_AGENTS)
        self._notify = notify
        self._session_id = session_id
        self._max_steps = max(1, int(max_steps))
        self._intent_provider = intent_provider
        # PRESENTATION turn identity — must equal TurnStarted.turn_id (the store's active artifact
        # id), because the TUI matrix drops any child update whose parent_turn_id differs (its
        # stale-callback protection). A task id here silently freezes the matrix.
        self._turn_id_fn = turn_id_fn
        self._work_provider = work_provider
        self._model_id = model_id or str(getattr(llm, "model", "") or "")
        # Detached-delegation owner (BackgroundChildManager); None disables background spawns.
        self._background = background
        self._lock = threading.Lock()
        self._n = 0                      # launch ordinal, per host (= per session)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    # ── schema ────────────────────────────────────────────────────────────────────────────────────
    def schemas(self):
        return list(self._inner.schemas()) + ([self._agent_schema()] if self.agents else [])

    def _agent_schema(self) -> dict:
        kinds = "; ".join(f"{n} ({sp.description})" for n, sp in self.agents.items())
        return {"type": "function", "function": {
            "name": "spawn_agent",
            "description": (
                "Delegate a self-contained sub-task to a child agent that runs in its OWN bounded context "
                "and returns one complete normalized report (its transcript and reads never enter your "
                "context). The report arrives directly in this tool result; an archive locator is optional.\n"
                "• agent = which KIND — " + kinds + ". For BREADTH (review/understand a repo, find a bug, "
                "audit several modules), explorers are read-only and independent scopes may run in parallel. "
                "Map and source-weight the work first; keep a review child near 20–30k source tokens, pass "
                "its exact path set in scope. Waves of 2–3 are concurrency windows, not scope boundaries: "
                "later-wave partitions must actually be launched before broad coverage is claimed. Never "
                "announce a fixed future wave that exists only in prose; call conditional later breadth an "
                "adaptive first pass. Do not create one child per directory or ask a child to read an entire "
                "large repository. If the user requested an exact child count or parallel shape, honor that "
                "total. Stay single-agent for one tightly-coupled change you're editing yourself."),
            "parameters": {"type": "object", "properties": {
                "agent": {"type": "string", "enum": list(self.agents),
                          "description": "the KIND to run (one of the live values in this schema)"},
                "task": {"type": "string", "description": "the self-contained sub-task for that agent"},
                "work_item_id": {"type": "string", "description": (
                    "OPTIONAL: bind this child to one open ACTIVE WORK item — its acceptance contract "
                    "(done_when/verify) is injected into the brief and the sealed report records the "
                    "binding")},
                "scope": _SCOPE_PARAM, "exclusions": _EXCLUSIONS_PARAM,
                "background": {"type": "boolean", "description": (
                    "OPTIONAL: run this child DETACHED (read-only kinds only) — the call returns "
                    "immediately and the report arrives later as a peer notification. Use for a long "
                    "breadth run so you can keep working or answer the user meanwhile; never wait, "
                    "poll, or re-spawn a detached child.")},
            }, "required": ["agent", "task"]}}}

    # ── scheduling contract ───────────────────────────────────────────────────────────────────────
    def accesses(self, name: str, args: dict) -> list:
        """Read-only kinds advertise ReadAllAccess → sibling children overlap in the read wave.
        Writable kinds advertise AllAccess → globally exclusive barrier (serialized, no worktrees)."""
        if name == "spawn_agent":
            spec = self.agents.get(str((args or {}).get("agent") or ""))
            if spec is not None and spec.read_only:
                return [ReadAllAccess()]
            return [AllAccess()]
        return self._inner.accesses(name, args)

    # ── dispatch ──────────────────────────────────────────────────────────────────────────────────
    def run(self, name, args):
        if name == "spawn_agent":
            return self._spawn(args if isinstance(args, dict) else {})
        return self._inner.run(name, args)

    def preflight_run(self, name, args):
        if name == "spawn_agent":
            return None, None          # validation lives in the handler; no admission object needed
        preflight = getattr(self._inner, "preflight_run", None)
        run_preflighted = getattr(self._inner, "run_preflighted", None)
        if callable(preflight) != callable(run_preflighted):
            # Preserve the kernel's incomplete-protocol rejection — never paper over half a protocol.
            return None, ToolText(
                "Error: wrapped tool host exposes an incomplete one-shot preflight protocol",
                status=ToolStatus.FAILED)
        return preflight(name, args) if callable(preflight) else (None, None)

    def run_preflighted(self, name, args, admission):
        if name == "spawn_agent":
            return self._spawn(args if isinstance(args, dict) else {})
        inner = getattr(self._inner, "run_preflighted", None)
        return inner(name, args, admission) if callable(inner) else self._inner.run(name, args)

    # ── the delegation ────────────────────────────────────────────────────────────────────────────
    def _brief(self, task: str, args: dict):
        """Assemble the child brief; returns (text, error ToolText | None)."""
        parts = [task]
        scope = args.get("scope") or ()
        scope = (scope,) if isinstance(scope, str) else tuple(str(s) for s in scope if str(s).strip())
        if scope:
            parts.append("## scope (exact areas in scope)\n" + "\n".join(f"- {s}" for s in scope))
        exclusions = args.get("exclusions") or ()
        exclusions = ((exclusions,) if isinstance(exclusions, str)
                      else tuple(str(e) for e in exclusions if str(e).strip()))
        if exclusions:
            parts.append("## exclusions (report rather than cross)\n"
                         + "\n".join(f"- {e}" for e in exclusions))
        if self._intent_provider is not None:
            # A configured provenance seam failing must BLOCK delegation; silently dropping binding
            # constraints would create a deceptively successful but under-scoped child report.
            intent = (self._intent_provider(task) if callable(self._intent_provider)
                      else self._intent_provider)
            constraints = _standing_constraints(intent)
            if constraints:
                parts.append("## standing constraints (from the user, verbatim — binding)\n"
                             + "\n".join(f"- {c}" for c in constraints))
        work_item_id = str(args.get("work_item_id") or "").strip()
        if work_item_id:
            graph = (self._work_provider() if callable(self._work_provider)
                     else self._work_provider)
            item = graph.get(work_item_id) if graph is not None else None
            if item is None:
                return "", _steered(
                    f"work_item_id {work_item_id!r} does not name a live ACTIVE WORK item; "
                    "call update_work to inspect the frontier, or spawn without a binding.")
            contract = [f"## bound work item {work_item_id}"]
            if item.description:
                contract.append(f"- item: {item.description}")
            if item.done_when:
                contract.append(f"- done_when (acceptance): {item.done_when}")
            for cmd in item.verify:
                contract.append(f"- verify (host-run on completion): `{cmd}`")
            parts.append("\n".join(contract))
        return "\n\n".join(parts), None

    def _spawn(self, args: dict):
        kind = str(args.get("agent") or "").strip()
        spec = self.agents.get(kind)
        if spec is None:
            # A hallucinated kind is a request-shape correction (↷ steer), matching the old contract.
            return _steered(f"unknown agent kind {kind!r}; available: "
                            + ", ".join(sorted(self.agents)))
        task = str(args.get("task") or "").strip()
        if not task:
            return _steered("spawn requires a non-empty 'task' describing the self-contained sub-task")
        brief, err = self._brief(task, args)
        if err is not None:
            return err
        cancel = args.get(CHILD_CANCEL_SIGNAL_ARG)          # loop-injected; Event-like, parent-composed
        activity = args.get(CHILD_ACTIVITY_ARG)             # loop-injected; shared monotonic liveness cell
        invocation_id = str(args.get(CHILD_INVOCATION_ID_ARG) or "")
        request_ordinal = int(args.get(CHILD_REQUEST_ORDINAL_ARG) or 0)
        with self._lock:
            self._n += 1
            launch = self._n

        turn_id = ""
        if self._turn_id_fn is not None:
            try:
                turn_id = str(self._turn_id_fn() if callable(self._turn_id_fn) else self._turn_id_fn)
            except Exception:  # noqa: BLE001
                turn_id = ""
        emitter = None
        if self._notify is not None or activity is not None:
            emitter = _ProgressEmitter(
                self._notify, activity=activity, agent_id=f"{turn_id or 'turn'}:agent:{launch}",
                parent_turn_id=turn_id, launch_ordinal=launch, kind=spec.name, name=spec.name,
                depth=1, session_id=self._session_id, invocation_id=invocation_id,
                request_ordinal=request_ordinal, objective=task)

        if bool(args.get("background")):
            return self._spawn_background(
                spec, brief, task, cancel=cancel, activity=activity, emitter=emitter,
                launch=launch, invocation_id=invocation_id,
                work_item_id=str(args.get("work_item_id") or "").strip())

        result = run_scoped_agent(
            brief, tools=self._inner, llm=self._llm, retriever=self._retriever, memory=self._memory,
            allowed_tools=allowed_for(spec, self._inner), model_id=self._model_id,
            max_steps=self._max_steps, signal=cancel, reasoning=spec.reasoning or "",
            system_extra=spec.system_prompt, on_event=emitter,
            transport_activity=(
                (lambda *_args, **_kwargs: activity.touch()) if activity is not None else None
            ))
        if emitter is not None:
            emitter.settling()

        work_item_id = str(args.get("work_item_id") or "").strip()
        handle = self._seal(spec.name, brief, result, launch, work_item_id)
        effects = self._effects(result, spec.name, launch, work_item_id, handle,
                                invocation_id or f"child-{launch}")

        # A non-clean outcome names its REASON in the header: "partial" alone tells the parent that
        # something was cut short but not what to do about it (retry a token ceiling, don't retry a
        # filter). The reason is already sealed; surfacing it costs one word.
        detail = result.status
        if result.status != "ok" and result.stop_reason and result.stop_reason != result.status:
            detail = f"{result.status} · {result.stop_reason}"
        header = f"[child {launch} · {spec.name} · {detail} · {result.steps} steps]"
        locator = f'\n\nsealed: read_file("subagents/{handle}.md")' if handle else ""
        if result.status == "cancelled":
            return ToolText(f"{header}\nNot run to completion: the delegation was cancelled before "
                            "the child could report.", status=ToolStatus.CANCELLED, effects=effects)
        if result.status == "indeterminate":
            body = result.report or "(no report reached the parent)"
            return ToolText(
                f"{header}\nThe child's physical state is UNKNOWN (unconfirmed close/timeout) — "
                f"treat any partial output below as unverified.\n\n{body}{locator}",
                ok=False, status=ToolStatus.INDETERMINATE, effects=effects)
        body = result.report or "(the child produced no report)"
        return ToolText(f"{header}\n\n{body}{locator}", ok=result.status != "failed",
                        effects=effects)

    # ── detached (background) delegation ─────────────────────────────────────────────────────────
    def _spawn_background(self, spec, brief, task, *, cancel, activity, emitter, launch,
                          invocation_id, work_item_id):
        """Start the child on a manager-owned thread and return a typed ``running`` outcome NOW.

        The terminal state re-enters the parent through two typed channels once the child settles:
        a SubagentProgress terminal update (matrix row) and a PeerMessage on the steer queue (the
        report, peer-enveloped). The in-band ToolResult only declares ``running`` so the progress
        reducer leaves the live row alone.
        """
        if not spec.read_only:
            return _steered(
                "background delegation is limited to read-only kinds — a detached writable child "
                "would mutate the workspace concurrently with you; run it in the foreground")
        manager = self._background
        if manager is None:
            return _steered("this host does not support background delegation; "
                            "rerun without 'background'")
        if not manager.has_capacity():
            return _steered(
                "background capacity is full (too many detached children still running); wait for "
                "a completion notification or run this one in the foreground")
        if cancel is None:
            cancel = threading.Event()      # watchdog-reachable even without a loop-injected lease
        # The row must exist BEFORE the spawn ToolResult can settle: once the call stops being an
        # active agent tool, the matrix rejects a first-time row arriving from the child thread.
        if emitter is not None:
            emitter.announce("starting", "running in background")
        timer = threading.Timer(background_absolute_timeout(),
                                getattr(cancel, "request", None) or cancel.set)
        timer.daemon = True

        def _run():
            try:
                result = run_scoped_agent(
                    brief, tools=self._inner, llm=self._llm, retriever=self._retriever,
                    memory=self._memory, allowed_tools=allowed_for(spec, self._inner),
                    model_id=self._model_id, max_steps=self._max_steps, signal=cancel,
                    reasoning=spec.reasoning or "", system_extra=spec.system_prompt,
                    on_event=emitter,
                    transport_activity=(
                        (lambda *_args, **_kwargs: activity.touch()) if activity is not None else None
                    ))
                if emitter is not None:
                    emitter.settling()
                handle = self._seal(spec.name, brief, result, launch, work_item_id)
                self._background_settled(spec, result, launch=launch, handle=handle,
                                         invocation_id=invocation_id, emitter=emitter,
                                         manager=manager)
            except Exception as exc:  # noqa: BLE001 — a detached child must never crash the host
                try:
                    if emitter is not None:
                        emitter.terminal("failed", f"background child error: {type(exc).__name__}")
                    manager.deliver(PeerMessage(
                        message_id=f"{invocation_id or f'child-{launch}'}:settled",
                        peer_id=f"child-{launch}",
                        content=(f"[child {launch} · {spec.name} · failed · background report]\n"
                                 f"The detached child crashed before it could report: "
                                 f"{type(exc).__name__}: {exc}"),
                        correlation_id=invocation_id, wake="none"))
                except Exception:  # noqa: BLE001
                    pass
            finally:
                timer.cancel()

        if not manager.start(_run, name=f"bg-child-{launch}"):
            timer.cancel()
            return _steered(
                "background capacity is full (too many detached children still running); wait for "
                "a completion notification or run this one in the foreground")
        timer.start()
        identity = invocation_id or f"child-{launch}"
        effects = (ToolEffect(f"{identity}:child-outcome", "child_outcome", {
            "status": "running", "operational_status": "running",
            "kind": spec.name, "name": spec.name, "launch_ordinal": launch,
            **({"work_item_id": work_item_id} if work_item_id else {}),
        }),)
        return ToolText(
            f"[child {launch} · {spec.name} · running in background]\n"
            "The child is detached. Its report arrives as a peer notification when it settles — do "
            "NOT wait, poll, or re-spawn it. Continue other work or answer the user now; integrate "
            "the report when the notification lands.",
            effects=effects)

    def _background_settled(self, spec, result, *, launch, handle, invocation_id, emitter,
                            manager):
        """Publish the detached child's terminal state: matrix settle first (same typed labels the
        in-band ToolResult would have carried), then the report through the peer channel."""
        detail = result.status
        if result.status != "ok" and result.stop_reason and result.stop_reason != result.status:
            detail = f"{result.status} · {result.stop_reason}"
        phase = {
            "partial": "partial", "failed": "failed", "cancelled": "cancelled",
            "indeterminate": "indeterminate",
        }.get(result.status) or (
            "report_ready" if result.report_completion in ("complete", "partial") else "completed"
        )
        if emitter is not None:
            emitter.terminal(phase, detail.replace("_", " "),
                             evidence_status=result.explorer_evidence_status,
                             report_completion=result.report_completion)
        manager.account_usage(result.usage or {})
        header = (f"[child {launch} · {spec.name} · {detail} · {result.steps} steps · "
                  "background report]")
        locator = f'\n\nsealed: read_file("subagents/{handle}.md")' if handle else ""
        body = result.report or "(the child produced no report)"
        # PeerMessage bodies are bounded (_PEER_CONTENT_MAX); the sealed artifact always holds the
        # full report, so a truncated preview degrades to the locator, never to data loss.
        budget = max(1000, 7900 - len(header) - len(locator))
        truncated = len(body) > budget
        content = header + "\n\n" + body[:budget] + locator
        if truncated:
            content += "\n[preview truncated — the locator above holds the complete report]"
        manager.deliver(PeerMessage(
            message_id=f"{invocation_id or f'child-{launch}'}:settled",
            peer_id=f"child-{launch}", content=content,
            correlation_id=invocation_id, wake="none"))

    def _effects(self, result, kind: str, launch: int, work_item_id: str, handle: str,
                 identity: str) -> tuple:
        """The TYPED lane beside the prose, derived wholly from ScopedResult.to_record().

        The parent needs three things the report body cannot carry: the child's token usage (the
        loop folds ``model_usage`` into the turn budget — without it a fan-out overruns
        AGENT_MAX_TOKENS unseen), report completeness (the progress reducer counts "reports ready"
        from ``report_completion``), and the stop cause. All three regressed to silence when this
        host returned prose alone.
        """
        record = result.to_record()
        outcome = {k: v for k, v in record.items() if k != "report"}   # body travels once, as prose
        outcome.update({"kind": kind, "name": kind, "launch_ordinal": launch})
        if work_item_id:
            outcome["work_item_id"] = work_item_id
        if handle:
            outcome["report_handle"] = f"subagents/{handle}.md"
            outcome["artifact_id"] = handle
        effects = [ToolEffect(f"{identity}:child-outcome", "child_outcome", outcome)]
        if handle:
            # A SEPARATE `child_artifact` effect, not the same facts folded into child_outcome:
            # every consumer keys on the effect KIND, so payload-level equivalence buys nothing.
            # scheduler._child_publication_committed treats this effect as PROOF that the child's
            # report was durably published — without it a wave cutoff may relabel an
            # already-sealed child's completed work as lost. receipts/event_ledger/cli journal it.
            effects.append(ToolEffect(f"{identity}:child-artifact", "child_artifact", {
                **{k: outcome[k] for k in ("status", "stop_reason", "stop_cause",
                                           "report_completion", "report_bytes",
                                           "explorer_evidence_status") if k in outcome},
                "artifact_id": handle,
                "report_handle": f"subagents/{handle}.md",
                **({"work_item_id": work_item_id} if work_item_id else {}),
            }))
        usage = record.get("usage") or {}
        if any(usage.values()):
            effects.append(ToolEffect(f"{identity}:model-usage", "model_usage",
                                      Usage.from_value(usage).as_dict()))
        return tuple(effects)

    def _seal(self, kind: str, brief: str, result, launch: int, work_item_id: str) -> str:
        """Dumb seal: ONE redacted JSON record through the existing archive; '' on any failure —
        the report already reached the parent inline, so persistence is an observer, never a gate."""
        append = getattr(self._memory, "append_subagent_artifact", None)
        if not callable(append) or not self._session_id:
            return ""
        # Same single projection as the typed effects — the durable record and the live one cannot
        # drift, because neither is hand-assembled.
        artifact = dict(result.to_record())
        artifact.update({"kind": kind, "launch_ordinal": launch, "brief": {"task": brief}})
        if work_item_id:
            artifact["work_item_id"] = work_item_id
        try:
            handle = append(self._session_id, artifact)
        except Exception:  # noqa: BLE001 — the report already reached the parent inline
            return ""
        if handle:
            index = getattr(self._memory, "index_subagent_artifact", None)
            if callable(index):
                try:
                    index(self._session_id, handle, artifact)
                except Exception:  # noqa: BLE001 — the FTS mirror is additive
                    pass
        return handle
