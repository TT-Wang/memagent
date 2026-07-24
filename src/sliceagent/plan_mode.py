"""PLAN MODE — a host-enforced read-only planning turn (P1 of docs/PLAN-MODE-DESIGN.md).

The plan IS Active Work: a planning turn explores the repo through read-only tools and delivers its plan
as `update_work` items carrying an acceptance contract (`verify` commands + `done_when`), fixed at plan
time so execution cannot lower the bar. There is deliberately no plan document, no second state store,
and no host-side planner — the model plans; the host owns the read-only regime.

Prompt discipline is ported from TT-Wang/forge `agents/planner.md` (MIT, same author), adapted to
sliceagent's tool surface and Active-Work substrate.
"""
from __future__ import annotations

from .agents import READ_ONLY_TOOLS
from .execution import ToolStatus
from .registry import ToolText

# Tools a planning turn may execute. Reads ground the plan; update_work IS the plan; ask_user resolves
# genuine ambiguity before commitments are written.
PLANNING_TOOLS = frozenset(READ_ONLY_TOOLS) | {"update_work", "ask_user"}

# Mode switches are palette state, never planning objectives: `/plan off` must not compose a planning
# TURN whose objective is the literal word "off".
_PLAN_SWITCHES = frozenset({"on", "off", "stop", "end", "exit", "cancel", "done"})

# "plan looks good" / "plan is fine" is talk ABOUT a plan, not a request for one — and it is the most
# likely phrasing at the exact moment a plan is on screen. A copula/verdict head disqualifies the
# slashless trigger; `/plan looks good` (explicit) is still honored as an objective.
_PLAN_COMMENTARY_HEADS = frozenset({
    "is", "was", "isn't", "looks", "look", "looked", "seems", "seem", "seemed", "sounds", "sound",
    "sounded", "works", "worked", "reads", "makes", "lgtm", "good", "great", "fine", "ok", "okay",
})

# Whole-message approvals that END sticky planning. Matched against the ENTIRE message on purpose:
# auto-exit is the direction that restores write access, so a longer message merely starting with
# "go" ("go read auth.py first") keeps planning armed. "go" is exactly what build_plan_prompt asks
# the user to reply, so the mode's own instructions are its exit.
PLAN_APPROVALS = frozenset({
    "go", "go ahead", "go for it", "do it", "execute", "run it", "proceed", "ship it",
    "approved", "approve", "lgtm", "yes go", "ok go", "okay go",
    "开始", "执行", "动手", "可以了",
})
# NOTE: no "/go" alias — every input path routes a leading "/" to the command palette before the
# turn transform sees it, so a slash approval would be a dead entry that silently does nothing.


def _plan_body(text: str) -> str | None:
    """The text after a leading `/plan` or `plan` token, or None when this is not a plan request."""
    raw = " ".join(str(text or "").split())
    lowered = raw.lower()
    for prefix in ("/plan ", "plan "):
        if lowered.startswith(prefix):
            return raw[len(prefix):].strip()
    return None


def plan_objective(text: str, *, armed: bool = False) -> str:
    """The objective of a planning request, or "" when this is not one.

    Accepts `/plan <objective>` and the leading-imperative `plan <objective>`, so natural phrasing
    arms the mode without a slash. Deliberately NOT a keyword search: only the LEADING position
    counts, because a mid-sentence "the plan" would silently restrict the agent and leave the user
    wondering why nothing happened.

    Two guards keep the slashless form from firing on talk ABOUT a plan — the likeliest phrasing
    right when the user is reviewing one:
      * a commentary predicate ("plan looks good", "plan is fine") is never an objective;
      * while ``armed``, the slashless form is inert entirely — mid-planning input belongs to the
        planning turn, not to a fresh re-plan. Explicit ``/plan <objective>`` still re-plans.
    """
    raw = " ".join(str(text or "").split())
    body = _plan_body(raw)
    if not body or body.lower() in _PLAN_SWITCHES:
        return ""
    if not raw.lower().startswith("/plan "):
        if armed:
            return ""
        head = body.split(" ", 1)[0].lower().strip(",.:;!?")
        if head in _PLAN_COMMENTARY_HEADS:
            return ""
    return body


def plan_switch(text: str) -> str:
    """`"on"` / `"off"` for an explicit mode switch (`/plan off`), else `""`."""
    body = _plan_body(text)
    if body is None or body.lower() not in _PLAN_SWITCHES:
        return ""
    return "on" if body.lower() == "on" else "off"


def is_plan_approval(text: str) -> bool:
    """True when the whole message is the approval that ends planning and starts execution."""
    return " ".join(str(text or "").lower().split()).strip(" .!?。！~～") in PLAN_APPROVALS

_STEER = (
    "planning mode — {name} is proposed, not executed. Capture the step as a work item instead "
    "(update_work: description + verify command + done_when); execution starts after the user "
    "approves the plan."
)


_READY_STEER = (
    "planning mode — items are PROPOSED here, never executed: create them as status 'open' (or "
    "'in_progress' for the first step) with their verify/done_when contract as data. Transitions to "
    "ready/delivered (which trigger host-run verify commands) happen during execution, after the user "
    "approves the plan."
)


class PlanningSurface:
    """Read-only projection of the workspace tool host for one planning turn.

    Mutating calls are steered (rendered ↷, never ✗ — nothing failed, the mode redirects) and provably
    cannot reach the inner host. Everything else delegates unchanged, so reads/provenance/journaling
    behave exactly as in a normal turn.

    The gate is enforced on EVERY dispatch protocol the loop can use — plain ``run`` AND the one-shot
    ``preflight_run``/``run_preflighted`` pair (run_tool_batch prefers the pair when a host exposes it;
    a wrapper that filtered only ``run`` would be silently bypassed by ``__getattr__`` delegation).
    ``update_work`` is additionally gated: a change landing an item on 'ready'/'delivered' would trigger
    the host's verify-command execution (real shell) — during planning that is steered, so a plan turn
    can only PROPOSE items, and no host command can run before the user approves the plan.
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _gate(self, name: str, args: dict):
        """The single mode gate: a steer ToolText for a disallowed call, else None."""
        if name not in PLANNING_TOOLS:
            return ToolText(_STEER.format(name=name), status=ToolStatus.CANCELLED)
        if name == "update_work":
            changes = args.get("changes") if isinstance(args, dict) else None
            for change in (changes if isinstance(changes, list) else []):
                status = str(change.get("status") or "") if isinstance(change, dict) else ""
                if status in ("ready", "delivered", "verified"):
                    return ToolText(_READY_STEER, status=ToolStatus.CANCELLED)
        return None

    def schemas(self) -> list:
        return [schema for schema in self._inner.schemas()
                if schema.get("function", {}).get("name") in PLANNING_TOOLS]

    def run(self, name: str, args: dict):
        steer = self._gate(name, args)
        if steer is not None:
            return steer
        return self._inner.run(name, args)

    # ── one-shot preflight protocol (both-or-neither: run_tool_batch requires the pair) ─────────
    def preflight_run(self, name: str, args: dict):
        steer = self._gate(name, args)
        if steer is not None:
            # A mode steer settles at preflight (never enters the started boundary), matching the
            # child-surface pattern; validation != None short-circuits execution in run_tool_batch.
            return None, steer
        inner = getattr(self._inner, "preflight_run", None)
        if callable(inner):
            return inner(name, args)
        return None, None

    def run_preflighted(self, name: str, args: dict, admission):
        steer = self._gate(name, args)
        if steer is not None:
            return steer
        inner = getattr(self._inner, "run_preflighted", None)
        if callable(inner):
            return inner(name, args, admission)
        return self._inner.run(name, args)


def build_plan_prompt(objective: str) -> str:
    """Compose the planning-turn request: the user's objective + the ported planning discipline."""
    objective = " ".join(str(objective or "").split())
    return f"""PLANNING MODE (host-enforced read-only turn) — produce an execution plan; do NOT execute.

OBJECTIVE: {objective}

The host has restricted this turn to read-only tools plus update_work. Any mutating call is steered, so
explore as deeply as needed — nothing can be touched.

## Phase 1 — Understand (do not skip)
1. Read the project manifest (pyproject/package.json/Makefile) to confirm the stack, test runner, build
   and lint commands.
2. Map the structure (list_files/glob) and read the files the objective touches — read enough to cite
   real symbols and line-level reality, not guesses (for non-trivial objectives that means several files).
3. Recall prior lessons TWICE: search_history with the objective's keywords, and once more for generic
   failure patterns (e.g. "regression", "clobber", "migration") — framework-shaped failures recur across
   unrelated tasks and keyword-matching only the topic misses them.

## Phase 2 — Decompose
Break the objective into 2–7 work items. Each item must:
- touch at most ~5 files (split if larger) and hold exactly one concern;
- be independently verifiable, with accurate `add_dependencies` edges;
- carry the acceptance contract: at least one `verify` command (prefer the project's existing test
  infrastructure; if none exists, build + lint + a runtime check) and a one-line `done_when`.
Discipline rules (each has a production scar behind it):
- Two parallel items must not edit the same file — merge or serialize them, and say so in the summary.
- For any file slated for DELETION: grep for its importers first; every importer must be covered by an
  item, or the deletion item must rewire them explicitly.
- When a new item calls an external CLI/API the codebase already uses: grep for the canonical
  timeout/auth/retry constants and reference them — never re-declare fresh values.
- Items with dependencies need at least one INTEGRATION verify (proves the pieces wire together), not
  only isolated checks.
- For refactors, end with a "verify no regressions" item running the full suite.
- Parser/extractor items must verify against REAL production-shaped input, not only synthesized fixtures.

## Phase 3 — Deliver
1. Create the COMPLETE item frontier via update_work in one atomic batch — every planned step as an item
   with description, dependencies, files (add_resources), verify, done_when. Never leave later phases
   only in prose.
2. Then present the plan to the user in text: the steps in order, the risks you found while reading,
   file-overlap or blast-radius warnings, and the verification strategy.
3. End with exactly this line so approval is unambiguous:
   Reply "go" to execute this plan, or tell me what to change.
"""
