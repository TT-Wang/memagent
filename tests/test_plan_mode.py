"""PLAN MODE (P1) — the host-enforced read-only planning turn.

The load-bearing guarantee is the ZERO-MUTATION LAW: a planning turn provably cannot reach a mutating
tool. Steers render ↷ (CANCELLED), never ✗ — nothing failed; the mode redirects. The plan lands as
Active Work items carrying the acceptance contract (verify/done_when), fixed at plan time.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent.execution import ToolStatus                       # noqa: E402
from sliceagent.plan_mode import (                                # noqa: E402
    PLANNING_TOOLS, PlanningSurface, build_plan_prompt,
)
from sliceagent.registry import ToolText                          # noqa: E402

CHECKS = []
def check(fn):
    CHECKS.append(fn)
    return fn


class _StubHost:
    """Records every call that reaches the inner host — the mutation detector."""

    def __init__(self):
        self.calls = []

    def schemas(self):
        names = ("read_file", "grep", "glob", "list_files", "search_history", "skill", "code_review",
                 "update_work", "ask_user",
                 "edit_file", "str_replace", "append_to_file", "run_command", "execute_code",
                 "spawn_agent", "write_memory", "change_workspace")
        return [{"type": "function", "function": {"name": n, "parameters": {}}} for n in names]

    def run(self, name, args):
        self.calls.append((name, args))
        return ToolText(f"{name} ok")

    def root(self):
        return "/tmp/stub-root"


@check
def zero_mutation_law_no_mutating_call_ever_reaches_the_host():
    inner = _StubHost()
    surface = PlanningSurface(inner)
    mutating = ("edit_file", "str_replace", "append_to_file", "run_command", "execute_code",
                "spawn_agent", "write_memory", "change_workspace")
    for name in mutating:
        out = surface.run(name, {"path": "x", "command": "rm -rf /"})
        assert isinstance(out, ToolText), name
        assert out.status is ToolStatus.CANCELLED, f"{name} must steer (↷), got {out.status}"
        assert "planning" in str(out), f"steer must name the mode: {out!r}"
    assert inner.calls == [], f"a mutating call reached the host during planning: {inner.calls}"


@check
def reads_and_the_plan_channel_delegate_unchanged():
    inner = _StubHost()
    surface = PlanningSurface(inner)
    for name in ("read_file", "grep", "update_work", "ask_user"):
        out = surface.run(name, {"q": 1})
        assert str(out) == f"{name} ok"
    assert [c[0] for c in inner.calls] == ["read_file", "grep", "update_work", "ask_user"]


@check
def schemas_advertise_only_the_planning_surface():
    names = {sch["function"]["name"] for sch in PlanningSurface(_StubHost()).schemas()}
    assert names == (names & PLANNING_TOOLS), f"leaked mutating schemas: {names - PLANNING_TOOLS}"
    assert "update_work" in names and "read_file" in names
    assert "edit_file" not in names and "run_command" not in names and "spawn_agent" not in names


@check
def surface_delegates_unknown_attributes_to_the_inner_host():
    surface = PlanningSurface(_StubHost())
    assert surface.root() == "/tmp/stub-root"   # loop/host plumbing must keep working through the wrapper


@check
def plan_prompt_carries_objective_and_the_contract_discipline():
    prompt = build_plan_prompt("  add   rate limiting  ")
    assert "add rate limiting" in prompt
    for marker in ("read-only", "update_work", "verify", "done_when", "2–7", "Reply \"go\""):
        assert marker in prompt, f"planning prompt lost its {marker!r} discipline"


@check
def work_item_acceptance_contract_roundtrips():
    from sliceagent.active_work import WorkGraph, WorkItem, WorkDelta
    graph = WorkGraph().open_request("event-1", "plan something", logical_id="log-1")
    root = graph.request_roots[-1]
    item = WorkItem(
        id="step-1", root_id=root.id, source_refs=root.source_refs,
        description="add limiter", status="open",
        verify=("pytest -q tests/test_limiter.py", "ruff check src/"),
        done_when="limiter tests green and no new lint errors",
    )
    graph = graph.apply(WorkDelta(expected_revision=graph.revision, creates=(item,)))
    thawed = type(graph).from_dict(graph.to_dict())
    got = next(it for it in thawed.items if it.id == "step-1")
    assert got.verify == item.verify and got.done_when == item.done_when
    # legacy records without the fields still load (tolerant reload, L2b)
    record = item.to_dict(); record.pop("verify"); record.pop("done_when")
    legacy = WorkItem.from_dict(record)
    assert legacy.verify == () and legacy.done_when == ""


@check
def update_work_intake_accepts_and_bounds_the_contract():
    from sliceagent.active_work import WorkGraph
    from sliceagent.tools import build_work_delta
    graph = WorkGraph().open_request("event-1", "plan it", logical_id="log-1")
    delta = build_work_delta(graph, {"expected_revision": graph.revision, "changes": [{
        "id": "s1", "description": "do the thing",
        "verify": ["pytest -q", "pytest -q"],          # dedup
        "done_when": "suite green",
    }]}, logical_id="log-1", workspace_epoch=0)
    (created,) = delta.creates
    assert created.verify == ("pytest -q",) and created.done_when == "suite green"
    try:
        build_work_delta(graph, {"expected_revision": graph.revision, "changes": [{
            "id": "s2", "description": "x", "verify": "not-a-list",
        }]}, logical_id="log-1", workspace_epoch=0)
        raise AssertionError("malformed verify must be rejected")
    except (TypeError, ValueError):
        pass



# ── P2: host-run item verification (Applied ≠ Verified) ────────────────────────────────────────

@check
def verification_green_promotes_ready_to_verified_and_model_still_cannot_forge_it():
    from sliceagent.active_work import WorkGraph
    from sliceagent.tools import build_work_delta, run_item_verification
    graph = WorkGraph().open_request("event-1", "do it", logical_id="log-1")
    graph = graph.apply_delta(build_work_delta(graph, {"expected_revision": graph.revision, "changes": [
        {"id": "s1", "description": "the step", "verify": ["true-cmd"], "done_when": "check green"},
    ]}, logical_id="log-1", workspace_epoch=0))

    green, failure = run_item_verification([("s1", ("true-cmd",))], lambda _c: (True, "ok"), {})
    assert green == frozenset({"s1"}) and failure == ""
    promoted = build_work_delta(graph, {"expected_revision": graph.revision, "changes": [
        {"id": "s1", "status": "ready"},
    ]}, logical_id="log-1", workspace_epoch=0, verified_ok=green)
    (item,) = promoted.updates
    assert item.status == "verified", "green host verify must promote ready -> verified"

    # the model still cannot supply 'verified' directly
    try:
        build_work_delta(graph, {"expected_revision": graph.revision, "changes": [
            {"id": "s1", "status": "verified"},
        ]}, logical_id="log-1", workspace_epoch=0)
        raise AssertionError("model-supplied 'verified' must stay forbidden")
    except (ValueError, TypeError):
        pass


@check
def verification_red_rejects_with_the_failing_output():
    from sliceagent.tools import run_item_verification
    green, failure = run_item_verification(
        [("s1", ("pytest -q",))], lambda _c: (False, "2 failed, 3 passed"), {},
    )
    assert green == frozenset()
    assert "verify failed for 's1'" in failure and "2 failed, 3 passed" in failure
    assert "Applied is not Verified" in failure


@check
def oscillation_same_failure_four_times_escalates_to_the_debugger():
    from sliceagent.tools import run_item_verification
    attempts = {}
    last = ""
    for _ in range(5):
        _, last = run_item_verification(
            [("s1", ("pytest -q",))], lambda _c: (False, "same exact failure"), attempts,
        )
    assert "debugger" in last, f"recurring identical failure must escalate: {last}"
    assert "not progress" in last
    # a DIFFERENT failure does not trip the oscillation counsel
    _, fresh = run_item_verification(
        [("s1", ("pytest -q",))], lambda _c: (False, "a brand new failure mode"), attempts,
    )
    assert "debugger" not in fresh


# ── the CRITICAL bypasses (review findings 1 & 2): drive the REAL loop dispatch, not surface.run ──

def _real_workspace_host(root):
    from sliceagent.tools import LocalToolHost
    return LocalToolHost(root=root)


@check
def zero_mutation_holds_through_the_loop_preflight_dispatch_path(tmp_path=None):
    """Finding #1: run_tool_batch prefers preflight_run/run_preflighted over run(); a wrapper filtering
    only run() was bypassed via __getattr__ delegation. Drive the ACTUAL dispatch path and assert a
    mutating call neither executes nor touches disk."""
    import tempfile
    import os
    from sliceagent.loop import run_tool_batch
    from sliceagent.hooks import Hooks
    from types import SimpleNamespace as NS
    root = tempfile.mkdtemp(prefix="plan-mutguard-")
    try:
        surface = PlanningSurface(_real_workspace_host(root))
        marker = os.path.join(root, "PWNED")
        call = NS(name="run_command", args={"command": f"touch {marker}"}, id="c1")
        _, rows = run_tool_batch([call], surface, lambda _e: None, Hooks(), step=1, turn_id="t")
        assert not os.path.exists(marker), "a mutating command EXECUTED during a planning turn"
        assert rows and rows[0]["outcome"].status is ToolStatus.CANCELLED, rows
        # a legit read still flows through the same path
        (open(os.path.join(root, "x.txt"), "w").write("hi"))
        read = NS(name="read_file", args={"path": "x.txt"}, id="c2")
        _, rrows = run_tool_batch([read], surface, lambda _e: None, Hooks(), step=2, turn_id="t")
        assert "hi" in str(rrows[0]["outcome"].text)
    finally:
        import shutil; shutil.rmtree(root, ignore_errors=True)


@check
def update_work_verify_commands_never_run_during_planning(tmp_path=None):
    """Finding #2: an item landing on 'ready' triggers host-run verify commands (real shell). During
    planning that must be steered — a plan turn PROPOSES items only, no command runs pre-approval."""
    import tempfile
    import os
    root = tempfile.mkdtemp(prefix="plan-verifyguard-")
    try:
        host = _real_workspace_host(root)
        from sliceagent.active_work import WorkGraph
        graph = WorkGraph().open_request("e", "obj", logical_id="l")
        host.bind_active_work(lambda g=graph: (g, "l", 0))
        surface = PlanningSurface(host)
        marker = os.path.join(root, "PWNED_VERIFY")
        out = surface.run("update_work", {"expected_revision": graph.revision, "changes": [{
            "id": "evil", "description": "x", "status": "ready",
            "verify": [f"touch {marker}"], "done_when": "x",
        }]})
        assert not os.path.exists(marker), "a verify command RAN during planning"
        assert out.status is ToolStatus.CANCELLED, out
        # the same call through the preflight protocol is also steered
        adm, validation = surface.preflight_run("update_work", {"expected_revision": graph.revision,
            "changes": [{"id": "evil2", "description": "x", "status": "delivered", "verify": [f"touch {marker}"]}]})
        assert validation is not None and validation.status is ToolStatus.CANCELLED
        assert not os.path.exists(marker)
        # a PROPOSED item (open/in_progress) with a verify contract stored as data is allowed
        ok = surface.run("update_work", {"expected_revision": graph.revision, "changes": [{
            "id": "step", "description": "do it", "status": "open",
            "verify": ["pytest -q"], "done_when": "green"}]})
        assert "accepted" in str(ok)
    finally:
        import shutil; shutil.rmtree(root, ignore_errors=True)


@check
def host_verified_promotion_actually_applies_through_the_graph(tmp_path=None):
    """Finding #3: ready->verified was rejected by the transition table, so the whole batch dropped.
    Drive the real _t_update_work end to end with a passing verify and assert the applied item is
    verified (and carries its typed proof)."""
    import tempfile
    root = tempfile.mkdtemp(prefix="plan-promote-")
    try:
        host = _real_workspace_host(root)
        from sliceagent.active_work import WorkGraph
        state = {"g": WorkGraph().open_request("e", "obj", logical_id="l")}
        host.bind_active_work(lambda: (state["g"], "l", 0))
        host._verify_runner = lambda _cmd: (True, "ok")   # inject a green runner

        def apply(args):
            # mirror the reducer: after a successful update_work, re-derive and apply the delta the effect
            # factory would (host verify memo replayed via verified_ok).
            from sliceagent.tools import build_work_delta
            out = host.run("update_work", args)
            green = frozenset(host._item_verify_green)
            delta = build_work_delta(state["g"], args, logical_id="l", workspace_epoch=0, verified_ok=green)
            state["g"] = state["g"].apply_delta(delta)
            return out

        apply({"expected_revision": state["g"].revision, "changes": [{
            "id": "s1", "description": "the step", "status": "in_progress",
            "verify": ["pytest -q"], "done_when": "green"}]})
        out = apply({"expected_revision": state["g"].revision, "changes": [{"id": "s1", "status": "ready"}]})
        assert "accepted" in str(out) and "Host-verified" in str(out), out
        item = next(it for it in state["g"].items if it.id == "s1")
        assert item.status == "verified", f"promotion did not apply: {item.status}"
        assert item.evidence_refs and item.output_refs, "verified item must carry typed proof"
    finally:
        import shutil; shutil.rmtree(root, ignore_errors=True)

@check
def plan_triggers_accept_slash_and_leading_imperative_only():
    """`/plan <obj>` and a LEADING `plan <obj>` arm the mode; a mid-sentence "plan" never does —
    a silent restriction the user did not ask for is worse than no trigger at all."""
    from sliceagent.plan_mode import plan_objective
    for armed, text in (
        ("the auth refactor", "/plan the auth refactor"),
        ("the auth refactor", "plan the auth refactor"),
        ("the auth refactor", "Plan the auth refactor"),
        ("migrate to v4", "  plan   migrate to v4  "),
    ):
        assert plan_objective(text) == armed, text
    for text in ("explain the plan", "what was the plan we discussed", "planning the migration",
                 "the plan looks good", "plan", "/plan", "replan the work", "go"):
        assert plan_objective(text) == "", f"{text!r} must NOT arm planning"


@check
def plan_switches_are_palette_state_not_planning_objectives():
    """`/plan off` must never mint a planning TURN whose objective is the literal word "off"."""
    from sliceagent.plan_mode import plan_objective, plan_switch
    for text in ("/plan off", "/plan on", "plan off", "/plan stop", "/plan cancel", "/plan done"):
        assert plan_objective(text) == "", f"{text!r} leaked into a planning turn"
    assert plan_switch("/plan on") == "on"
    for text in ("/plan off", "/plan stop", "/plan end", "/plan cancel", "/plan done", "plan off"):
        assert plan_switch(text) == "off", text
    assert plan_switch("/plan the auth refactor") == "" and plan_switch("go") == ""


@check
def only_a_whole_message_approval_ends_planning():
    """Auto-EXIT restores write access, so it stays exact-match: a longer message that merely
    STARTS with "go" keeps the mode armed."""
    from sliceagent.plan_mode import is_plan_approval
    for text in ("go", "GO", "go.", "  go  ", "go ahead", "do it", "proceed", "lgtm", "ship it",
                 "开始", "执行"):
        assert is_plan_approval(text), f"{text!r} must end planning"
    for text in ("go read auth.py first", "do it after you check the tests", "going to review",
                 "don't go yet", "go ahead and add a third item to the plan", ""):
        assert not is_plan_approval(text), f"{text!r} must KEEP planning armed"


@check
def the_turn_surface_follows_a_workspace_handoff():
    """P1 (adversarial review, pre-existing): `turn_tools` was captured ONCE before the segment
    loop, but a workspace handoff rebinds the host mid-loop. The continuation therefore executed
    against the workspace the user had just LEFT while its slice described the new one — and the
    seed was built from a different object than run_turn executed with, so a planning turn could
    advertise tools its own surface would steer.

    Models the loop's resolution order: the surface must be re-resolved per SEGMENT, and the seed
    must be built from the same object the turn executes with.
    """
    hosts = {"A": _StubHost(), "B": _StubHost()}
    live = {"tools": hosts["A"]}
    planning = {"active": False}

    def turn_tools():                       # cli._turn_tools — reads the CURRENT host every call
        return PlanningSurface(live["tools"]) if planning["active"] else live["tools"]

    def segment(handoff_to=None):
        """One segment: resolve the surface, build the seed from it, then execute with it."""
        surface = turn_tools()
        seed_host, exec_host = surface, surface      # the invariant: one object, not two
        if handoff_to:                                # change_workspace rebinds the live host
            live["tools"] = hosts[handoff_to]
        return seed_host, exec_host

    seed1, exec1 = segment(handoff_to="B")
    assert seed1 is exec1 is hosts["A"], "first segment must use workspace A"
    seed2, exec2 = segment()
    assert seed2 is exec2 is hosts["B"], (
        "the continuation still holds the OLD workspace's host — it would edit the workspace the "
        "user just left while claiming to work in the new one")

    # and while planning is armed the seed sees exactly the surface that will execute
    planning["active"] = True
    seed3, exec3 = segment()
    assert seed3 is exec3 and isinstance(seed3, PlanningSurface)
    assert "spawn_agent" not in {s["function"]["name"] for s in seed3.schemas()}, (
        "a planning turn must not advertise a tool its own surface steers")


@check
def a_planning_turn_can_never_execute_shell_through_status_inheritance():
    """ZERO-MUTATION LAW, second hole (adversarial review): the surface gate steers a REQUESTED
    ready/delivered/verified, but the host used to trigger verification on the RESULTING item state.
    So a change that merely INHERITED 'ready' (no status field) passed the gate and then ran real
    shell commands from inside a read-only planning turn. Verification now gates the TRANSITION.
    """
    import tempfile
    from sliceagent.active_work import WorkGraph
    from sliceagent.tools import build_work_delta
    root = tempfile.mkdtemp(prefix="plan-zero-mutation-")
    try:
        host = _real_workspace_host(root)
        state = {"g": WorkGraph().open_request("e", "obj", logical_id="l")}
        host.bind_active_work(lambda: (state["g"], "l", 0))
        ran = []
        host._verify_runner = lambda cmd: (ran.append(cmd), (True, "ok"))[1]

        def apply(args, surface=None):
            out = (surface or host).run("update_work", args)
            green = frozenset(host._item_verify_green)
            try:
                state["g"] = state["g"].apply_delta(build_work_delta(
                    state["g"], args, logical_id="l", workspace_epoch=0, verified_ok=green))
            except Exception:  # noqa: BLE001 — a rejected batch simply does not apply
                pass
            return out

        # an item sits at 'ready' with nothing to prove yet (legal, no shell)
        apply({"expected_revision": state["g"].revision,
               "changes": [{"id": "s1", "description": "step", "status": "ready"}]})
        assert ran == [], ran

        # a PLANNING turn adds the acceptance contract — its whole job — with NO status field
        apply({"expected_revision": state["g"].revision,
               "changes": [{"id": "s1", "verify": ["echo SHOULD-NOT-RUN"], "done_when": "green"}]},
              surface=PlanningSurface(host))
        assert ran == [], f"a read-only planning turn executed shell: {ran}"

        # and the legitimate TRANSITION into ready still verifies (the gate must not be a mute button)
        apply({"expected_revision": state["g"].revision,
               "changes": [{"id": "s2", "description": "s2", "status": "in_progress",
                            "verify": ["echo SHOULD-RUN"], "done_when": "green"}]})
        apply({"expected_revision": state["g"].revision,
               "changes": [{"id": "s2", "status": "ready"}]})
        assert ran == ["echo SHOULD-RUN"], f"the real transition stopped verifying: {ran}"
        assert next(i.status for i in state["g"].items if i.id == "s2") == "verified"
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


@check
def the_product_term_plan_mode_arms_from_natural_phrasing():
    """FIELD REGRESSION: "use plan mode to review the code again" silently did NOTHING — the trigger
    only matched a LEADING `plan `. The user saw no chip, no plan, no approval request, and the FULL
    tool surface stayed live, so the agent ran commands and spawned 14 children. A request that looks
    like it armed the mode but did not is the worst possible outcome: the user believes writes are
    impossible while they are not.

    "plan mode" is a product term nobody writes by accident, so it is honoured anywhere in the
    message — unlike the bare word "plan", which still needs the leading position + guards.
    """
    from sliceagent.plan_mode import plan_objective, plan_switch
    for text, expected in (
        ("use plan mode to review the code again", "review the code again"),
        ("use plan mode and review the code", "review the code"),
        ("review the code in plan mode", "review the code"),
        ("please use planning mode to audit auth", "audit auth"),
        ("switch to plan mode then refactor the parser", "refactor the parser"),
    ):
        assert plan_objective(text) == expected, (text, plan_objective(text))

    # the bare phrase is a SWITCH (arm the surface, no turn), not an empty-objective planning turn
    for text in ("enter plan mode", "use plan mode", "plan mode", "turn on plan mode"):
        assert plan_switch(text) == "on", text
        assert plan_objective(text) == "", text

    # and none of the guards regressed
    for text in ("plan looks good", "plan approved", "the plan is fine", "planning the migration",
                 "go"):
        assert plan_objective(text) == "" and plan_switch(text) == "", text


@check
def the_planning_discipline_never_impersonates_the_user_request():
    """ROOT CAUSE of a poisoned skill: the transform REPLACED the user's text with the planning
    prompt, so the archived CURRENT REQUEST became host prose. Consolidation then learned a skill
    titled "PLANNING MODE (host-enforced read-only turn)" whose taught process was
    append_to_file/edit_file/run_command — a skill that claims read-only and demonstrates writing.

    The discipline must ride the SYSTEM layer; the user's exact words stay the request.
    """
    import tempfile
    from sliceagent.memory import NullMemory
    from sliceagent.pfc import Slice, record_user
    from sliceagent.retriever import NullRetriever
    from sliceagent.seed import make_build_slice
    from sliceagent.tools import LocalToolHost

    user_text = "use plan mode to review the code again"
    overlay = build_plan_prompt("review the code again")
    state = Slice()
    state.reset(user_text)
    record_user(state, user_text, source_event_id="e1", logical_id="l1")
    messages = make_build_slice(
        state, LocalToolHost(root=tempfile.mkdtemp(prefix="plan-overlay-")), NullRetriever(),
        NullMemory(), user_text, "s-1", system_extra=overlay,
    )()
    system, request = str(messages[0].get("content", "")), str(messages[-1].get("content", ""))
    assert "PLANNING MODE" in system, "the discipline must still reach the model"
    assert user_text in request, "the user's exact words must remain the request"
    assert "PLANNING MODE" not in request, (
        "the host prompt is masquerading as the user's request — everything derived from history "
        "(consolidation, skills, recall) will learn from prose the user never wrote")
    assert "named subagent" not in system, (
        "a top-level planning turn is not a subagent; the overlay heading must not assert one")


@check
def slashless_plan_trigger_ignores_talk_about_a_plan():
    """The slashless form must not fire on talk ABOUT a plan — the likeliest phrasing at the exact
    moment a plan is on screen and the agent has just asked for approval. Every rejected case below
    was produced by adversarial review as a real false-arm."""
    from sliceagent.plan_mode import plan_objective
    for text in ("plan looks good", "plan is fine", "plan sounds great", "plan lgtm",
                 "plan seems right", "plan was wrong", "plan looks good, go ahead", "plan approved",
                 "plan accepted", "plan seems right, do it", "plan b is fine",
                 "plan on refactoring later", "plan go ahead"):
        assert plan_objective(text) == "", f"{text!r} is commentary, not a planning request"
    for text in ("plan the auth refactor", "plan for the migration", "plan to split the module",
                 "plan a rewrite of the parser", "plan how the cache is keyed",
                 "plan migrating v3 to v4"):
        assert plan_objective(text), f"{text!r} is a real planning request"
    # while armed, slashless input belongs to the planning turn; explicit /plan still re-plans
    assert plan_objective("plan another approach", armed=True) == ""
    assert plan_objective("/plan another approach", armed=True) == "another approach"


@check
def every_input_path_can_exit_the_sticky_mode():
    """P0 REGRESSION (adversarial review): the plain-input loop only reached the plan transform
    through a plan-shaped branch, so an approval ("go") never disarmed and `/plan off` worked only
    when the TUI extra was installed — arming was a ONE-WAY DOOR into read-only mode.

    This replays each path's BRANCH CHAIN, not just the transform, because the bug lived entirely in
    the dispatch order. The prior test mirrored the live path and could not see it.
    """
    from sliceagent.plan_mode import (
        build_plan_prompt, is_plan_approval, plan_objective, plan_switch,
    )

    def make_paths(tui_installed: bool):
        state = {"active": False}
        palette = []

        def transform(text):                      # cli._plan_mode_transform
            switch = plan_switch(text)
            if switch:
                state["active"] = switch == "on"
                return text
            objective = plan_objective(text)
            if objective:
                state["active"] = True
                return build_plan_prompt(objective)
            if state["active"] and is_plan_approval(text):
                state["active"] = False
            return text

        def handle_slash(line):                   # cli._handle_slash's /plan switch branch
            palette.append(line)
            switch = plan_switch(line)
            if switch:
                state["active"] = switch == "on"

        def plain_path(line):                     # cli.py plain-input loop, in branch order
            if plan_switch(line):
                transform(line)
                return "no-turn"
            line = transform(line)
            if line.startswith("/learn"):
                return "turn"
            if tui_installed and line.startswith("/"):
                handle_slash(line)
                return "no-turn"
            return "turn"

        def live_path(text):                      # tui.py handler → cli._run_one_turn
            if plan_switch(text):
                handle_slash(f"/plan {plan_switch(text)}")
                return "no-turn"
            if not plan_objective(text) and text.startswith("/"):
                handle_slash(text)
                return "no-turn"
            transform(text)
            return "turn"

        return state, plain_path, live_path

    for tui_installed in (True, False):
        for label, path in (("plain", 1), ("live", 2)):
            state, plain_path, live_path = make_paths(tui_installed)
            run = plain_path if path == 1 else live_path
            assert run("plan the auth refactor") == "turn"
            assert state["active"], f"{label}/tui={tui_installed}: arming failed"

            # THE BUG: an approval arrives as ordinary input — every path must process it.
            assert run("go") == "turn"
            assert not state["active"], (
                f"{label} path (tui={tui_installed}) cannot exit planning with an approval — "
                "sticky mode is a one-way door")

            # and the explicit switch never mints a turn, in either path
            state["active"] = True
            assert run("/plan off") == "no-turn", f"{label}: /plan off minted a turn"
            assert not state["active"], f"{label}: /plan off did not disarm (tui={tui_installed})"
            state["active"] = True
            assert run("plan off") == "no-turn", f"{label}: bare `plan off` minted a turn"
            assert not state["active"], f"{label}: bare `plan off` did not disarm"


@check
def no_approval_is_shadowed_by_the_command_palette():
    """Every input path routes a leading "/" to the palette before the turn transform, so a slash
    approval would be a dead entry that silently does nothing."""
    from sliceagent.plan_mode import PLAN_APPROVALS
    assert not [a for a in PLAN_APPROVALS if a.startswith("/")], sorted(PLAN_APPROVALS)


@check
def approvals_never_fall_into_the_chitchat_fast_path():
    """Cross-module invariant: _run_one_turn applies the plan transform and THEN checks the social
    fast path. If an approval were classified as chitchat, "go" would disarm planning and get a
    cheap social reply — the plan would never execute. Pins the two vocabularies apart."""
    from sliceagent.plan_mode import PLAN_APPROVALS
    from sliceagent.text_utils import is_chitchat
    for approval in PLAN_APPROVALS:
        if approval.startswith("/"):
            continue
        assert not is_chitchat(approval), (
            f"{approval!r} is both an approval and chitchat — the social fast path would swallow "
            "the plan's execution turn")


@check
def planning_mode_is_sticky_until_an_explicit_exit():
    """The regression this replaces: the one-shot flag silently restored write access on turn two,
    mid-iteration. Drives the real cli transform/surface pair through a multi-turn sequence."""
    from sliceagent.plan_mode import (
        PlanningSurface, build_plan_prompt, is_plan_approval, plan_objective, plan_switch,
    )
    inner = _StubHost()
    state = {"active": False, "stats": {}}

    def set_planning(active):     # mirrors cli._set_planning (flag + the visible toolbar chip)
        state["active"] = bool(active)
        state["stats"]["planning"] = bool(active)

    def transform(text):          # mirrors cli._plan_mode_transform
        switch = plan_switch(text)
        if switch:
            set_planning(switch == "on")
            return text
        objective = plan_objective(text)
        if objective:
            set_planning(True)
            return build_plan_prompt(objective)
        if state["active"] and is_plan_approval(text):
            set_planning(False)
        return text

    def turn_tools():             # mirrors cli._turn_tools (NO consume — that is the fix)
        return PlanningSurface(inner) if state["active"] else inner

    prompt = transform("plan the auth refactor")
    assert "PLANNING MODE" in prompt and state["active"]
    assert state["stats"]["planning"] is True, "the armed state must be visible in the toolbar"

    # turn 2: iterating on the plan — still read-only (the old one-shot consume failed HERE)
    transform("also cover the refresh-token path")
    assert state["active"], "planning must survive a follow-up turn"
    out = turn_tools().run("edit_file", {"path": "a.py", "content": "x"})
    assert out.status is ToolStatus.CANCELLED and inner.calls == [], inner.calls

    # turn 3: an approval releases the full surface, and the plan itself is Active Work (unrewritten)
    assert transform("go") == "go" and not state["active"]
    assert state["stats"]["planning"] is False
    assert turn_tools() is inner, "approval must restore the real host"
    turn_tools().run("edit_file", {"path": "a.py", "content": "x"})
    assert inner.calls == [("edit_file", {"path": "a.py", "content": "x"})]

    # and the explicit switch works without any objective
    transform("/plan on")
    assert state["active"] and isinstance(turn_tools(), PlanningSurface)
    transform("/plan off")
    assert not state["active"] and turn_tools() is inner


@check
def verify_commands_announce_live_on_the_status_line():
    """A long host-run verify (a real pytest run) must be VISIBLE, not a generic `update_work`:
    each command flows through _verify_notify into TurnProgress.host_activity → `running — verify ·
    <cmd>` on the live status line. Presentation only — a raising notify never gates verification."""
    import tempfile
    from sliceagent.events import TurnStarted
    from sliceagent.progress import ProgressPhase, TurnProgress

    root = tempfile.mkdtemp(prefix="plan-verify-notify-")
    try:
        host = _real_workspace_host(root)
        from sliceagent.active_work import WorkGraph
        state = {"g": WorkGraph().open_request("e", "obj", logical_id="l")}
        host.bind_active_work(lambda: (state["g"], "l", 0))
        host._verify_runner = lambda _cmd: (True, "ok")

        progress = TurnProgress(await_commit=False)
        progress.reduce(TurnStarted(request="r", task_id="t", turn_id="turn-1"))
        host._verify_notify = progress.host_activity

        def apply(args):
            from sliceagent.tools import build_work_delta
            out = host.run("update_work", args)
            green = frozenset(host._item_verify_green)
            delta = build_work_delta(state["g"], args, logical_id="l", workspace_epoch=0,
                                     verified_ok=green)
            state["g"] = state["g"].apply_delta(delta)
            return out

        apply({"expected_revision": state["g"].revision, "changes": [{
            "id": "v1", "description": "step", "status": "in_progress",
            "verify": ["pytest -q tests/x.py"], "done_when": "green"}]})
        apply({"expected_revision": state["g"].revision, "changes": [{"id": "v1", "status": "ready"}]})
        snap = progress.snapshot()
        assert snap.phase == ProgressPhase.RUNNING, snap.phase
        assert "verify · pytest -q tests/x.py" in snap.detail, snap.detail

        # a raising notify must never gate verification: the promotion still lands
        host._verify_notify = lambda _d: (_ for _ in ()).throw(RuntimeError("ui died"))
        host._verify_runner = lambda _cmd: (True, "ok")
        apply({"expected_revision": state["g"].revision, "changes": [{
            "id": "v2", "description": "s2", "status": "in_progress",
            "verify": ["true-cmd"], "done_when": "g2"}]})
        apply({"expected_revision": state["g"].revision, "changes": [{"id": "v2", "status": "ready"}]})
        item = next(it for it in state["g"].items if it.id == "v2")
        assert item.status == "verified", f"a raising notify gated verification: {item.status}"
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    ok = 0
    for fn in CHECKS:
        try:
            fn(); ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(CHECKS)} passed")
    sys.exit(0 if ok == len(CHECKS) else 1)
