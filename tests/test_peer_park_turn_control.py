"""The turn-ending peer park (task #104): a host tool can end a turn parked on a peer.

Control flow is recognised by TYPE, never by prose — a model cannot talk the kernel into
parking, and two parks in one batch is a typed conflict rather than a silent overwrite.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from sliceagent.execution import ToolPurity
from sliceagent.hooks import Hooks
from sliceagent.interfaces import PeerParkControl, PeerWait
from sliceagent.loop import run_turn
from sliceagent.registry import ToolEntry, ToolRegistry, TurnControlRegistrar

PARK = PeerWait(correlation_id="ask-1", peer_id="sre", deadline_s=None)


def _host(*handlers, exclusive_first=True):
    class Host:
        def __init__(self):
            self.registry = ToolRegistry()
            for index, handler in enumerate(handlers):
                name = f"tool_{index}"
                exclusive = index == 0 and exclusive_first
                entry = ToolEntry(
                    name=name,
                    schema={"type": "function", "function": {
                        "name": name,
                        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                    }},
                    handler=handler, source="host", purity=ToolPurity.UNKNOWN, deduplicable=False,
                    turn_exclusive=exclusive,
                )
                # Authority is the registrar the host picks, so the fake host must pick it too;
                # registering a control tool the ordinary way is exactly the unauthorized case.
                if exclusive:
                    TurnControlRegistrar(self.registry).register(entry)
                else:
                    self.registry.register(entry)

        def schemas(self):
            return []

        def run(self, name, args):
            return self.registry.entry(name).handler(args)

        def read_text(self, path):
            raise FileNotFoundError(path)

        def accesses(self, name, args):
            return []

    return Host()


def _llm(calls):
    class LLM:
        def __init__(self):
            self.seen = 0

        def complete(self, messages, tools):
            self.seen += 1
            if self.seen <= len(calls):
                names = calls[self.seen - 1]
                return NS(
                    content="",
                    tool_calls=[NS(name=n, id=f"c{i}", args={}) for i, n in enumerate(names)],
                    finish_reason="tool_calls", usage={},
                )
            return NS(content="done", tool_calls=[], finish_reason="stop", usage={})

    return LLM()


def _run(host, llm):
    return run_turn(
        build_slice=lambda: [{"role": "user", "content": "ask the collaborator"}],
        llm=llm, tools=host, dispatch=lambda e: None, hooks=Hooks(),
    )


def test_a_host_tool_can_end_the_turn_parked_on_a_peer():
    llm = _llm([["tool_0"]])
    result = _run(_host(lambda args: PeerParkControl(PARK)), llm)
    assert result.stop_reason == "waiting_peer"
    assert result.peer_wait == PARK
    # The turn ENDED at the park: the model was not called again.
    assert llm.seen == 1


def test_prose_cannot_park_a_turn():
    """The kernel recognises a park by type. Text that merely claims one must not park."""
    llm = _llm([["tool_0"]])
    result = _run(_host(lambda args: "PeerParkControl(waiting_peer) — parking now"), llm)
    assert result.stop_reason != "waiting_peer"
    assert result.peer_wait is None


def test_two_parks_in_one_batch_is_a_typed_conflict():
    """Exclusivity: a turn can wait on exactly one collaborator.

    Silently keeping one park would leave the other correlation permanently unanswerable.
    """
    llm = _llm([["tool_0", "tool_1"]])
    result = _run(
        _host(
            lambda args: PeerParkControl(PARK),
            lambda args: PeerParkControl(
                PeerWait(correlation_id="ask-2", peer_id="other", deadline_s=None)
            ),
        ),
        llm,
    )
    assert result.stop_reason != "waiting_peer"
    assert result.peer_wait is None


def test_an_ordinary_turn_still_reports_no_park():
    llm = _llm([])
    result = _run(_host(lambda args: "ok"), llm)
    assert result.stop_reason == "end_turn"
    assert result.peer_wait is None


def test_a_finite_deadline_is_refused_at_the_boundary():
    """MVP scope: a bounded park needs platform capability we do not have."""
    with pytest.raises(ValueError):
        PeerParkControl(PeerWait(correlation_id="ask-3", peer_id="sre", deadline_s=30.0))


# --------------------------------------------------------------------------------------
# The seal must persist the park durably, or the kernel reports waiting_peer while the
# durable record forgets it and the peer's reply has nothing to resume.
# --------------------------------------------------------------------------------------


def test_a_parked_turn_seals_the_park_into_the_durable_graph():
    from sliceagent.active_work import WorkGraph

    graph = WorkGraph().open_request("evt-1", "ask the SRE").seal_current(
        "waiting_peer", peer_wait=PARK
    )
    root = graph.request_roots[-1]
    assert root.status == "waiting_peer"
    assert root.peer_wait == PARK
    # And it survives the durable wire round-trip the checkpoint uses.
    restored = WorkGraph.from_records(graph.to_records()).request_roots[-1]
    assert restored.status == "waiting_peer"
    assert restored.peer_wait == PARK


def test_sealing_waiting_peer_without_the_park_is_refused():
    """The paired invariant: a parked status may never exist without its correlation."""
    from sliceagent.active_work import GraphValidationError, WorkGraph

    graph = WorkGraph().open_request("evt-1", "ask the SRE")
    with pytest.raises(GraphValidationError):
        graph.seal_current("waiting_peer")


def test_waiting_peer_is_a_typed_status_not_a_loose_string():
    from sliceagent.execution import TurnStatus, ToolStatus

    assert TurnStatus("waiting_peer") is TurnStatus.WAITING_PEER
    # It belongs to the TURN vocabulary only; a tool never has this status.
    assert not hasattr(ToolStatus, "WAITING_PEER")


def test_the_result_boundary_enforces_the_paired_invariant():
    """Either half alone is a lie: an unresumable park, or a park for finished work."""
    from sliceagent.execution import TurnOutcome

    with pytest.raises(ValueError):
        TurnOutcome("waiting_peer", 1, {})                      # parked, no wait
    with pytest.raises(ValueError):
        TurnOutcome("end_turn", 1, {}, peer_wait=PARK)          # wait, not parked


# --------------------------------------------------------------------------------------
# Whole-batch exclusivity, decided BEFORE any handler runs. Detecting a conflict after
# execution is too late: each handler may already have prepared/dispatched durable effects.
# --------------------------------------------------------------------------------------


def _counting(result):
    calls = []

    def handler(args):
        calls.append(1)
        return result

    handler.calls = calls
    return handler


def test_a_turn_exclusive_call_batched_with_a_sibling_runs_NO_handler():
    ask = _counting(PeerParkControl(PARK))
    sibling = _counting("side effect")
    llm = _llm([["tool_0", "tool_1"]])
    result = _run(_host(ask, sibling), llm)
    assert result.stop_reason != "waiting_peer"
    assert result.peer_wait is None
    # The point of the gate: ZERO effects, not "conflict detected afterwards".
    assert ask.calls == [] and sibling.calls == []


def test_two_turn_exclusive_calls_run_NO_handler():
    first = _counting(PeerParkControl(PARK))
    second = _counting(PeerParkControl(
        PeerWait(correlation_id="ask-2", peer_id="other", deadline_s=None)
    ))

    class Host2:
        def __init__(self):
            from sliceagent.registry import ToolRegistry as _R
            self.registry = _R()
            for i, h in enumerate((first, second)):
                TurnControlRegistrar(self.registry).register(ToolEntry(
                    name=f"tool_{i}",
                    schema={"type": "function", "function": {
                        "name": f"tool_{i}",
                        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                    }},
                    handler=h, source="host", purity=ToolPurity.UNKNOWN,
                    deduplicable=False, turn_exclusive=True,
                ))

        def schemas(self):
            return []

        def run(self, name, args):
            return self.registry.entry(name).handler(args)

        def read_text(self, path):
            raise FileNotFoundError(path)

        def accesses(self, name, args):
            return []

    result = _run(Host2(), _llm([["tool_0", "tool_1"]]))
    assert result.stop_reason != "waiting_peer"
    assert first.calls == [] and second.calls == []


def test_a_lone_turn_exclusive_call_still_parks():
    """The gate must not break the honest single-call case."""
    ask = _counting(PeerParkControl(PARK))
    result = _run(_host(ask), _llm([["tool_0"]]))
    assert result.stop_reason == "waiting_peer"
    assert ask.calls == [1]


# --------------------------------------------------------------------------------------
# PRODUCTION-SHAPED: the earlier tests used a fake host whose .run() returned the raw
# handler value. The real ToolRegistry converts a non-ToolText return to prose BEFORE
# finalize_tool_outcome, so the carrier has to survive that choke point or production
# would silently never park while the tests stayed green.
# --------------------------------------------------------------------------------------


class _RealHost:
    """Uses the real ToolRegistry.run() path, not a raw-handler shortcut."""

    def __init__(self, handler, *, name="ask_collaborator", exclusive=True):
        from sliceagent.registry import ToolRegistry
        self.registry = ToolRegistry()
        entry = ToolEntry(
            name=name,
            schema={"type": "function", "function": {
                "name": name,
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }},
            handler=handler, source="host", purity=ToolPurity.UNKNOWN,
            deduplicable=False, turn_exclusive=exclusive,
        )
        if exclusive:
            TurnControlRegistrar(self.registry).register(entry)
        else:
            self.registry.register(entry)

    def schemas(self):
        return []

    def run(self, name, args):
        return self.registry.run(name, args)

    def read_text(self, path):
        raise FileNotFoundError(path)

    def accesses(self, name, args):
        return []


def test_the_carrier_survives_the_real_registry_choke_point():
    host = _RealHost(lambda args: PeerParkControl(PARK))
    llm = _llm([["ask_collaborator"]])
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "ask"}],
        llm=llm, tools=host, dispatch=lambda e: None, hooks=Hooks(),
    )
    assert result.stop_reason == "waiting_peer"
    assert result.peer_wait == PARK


def test_the_park_is_presented_body_free_in_the_transcript():
    """The correlation must not be stringified into model-visible text."""
    from sliceagent.registry import ToolRegistry

    registry = ToolRegistry()
    TurnControlRegistrar(registry).register(ToolEntry(
        name="ask_collaborator",
        schema={"type": "function", "function": {
            "name": "ask_collaborator",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }},
        handler=lambda args: PeerParkControl(PARK), source="host",
        purity=ToolPurity.UNKNOWN, deduplicable=False, turn_exclusive=True,
    ))
    out = registry.run("ask_collaborator", {})
    assert out.control is not None                    # typed control preserved
    assert PARK.correlation_id not in str(out)        # identity not leaked into the transcript
    assert PARK.peer_id not in str(out)


def test_a_failed_control_never_parks():
    """A park on a call that did not succeed would wait forever for a reply nobody asked for."""
    from sliceagent.registry import ToolText

    host = _RealHost(lambda args: ToolText("could not reach the collaborator", ok=False,
                                           control=PeerParkControl(PARK)))
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "ask"}],
        llm=_llm([["ask_collaborator"]]), tools=host, dispatch=lambda e: None, hooks=Hooks(),
    )
    assert result.stop_reason != "waiting_peer"
    assert result.peer_wait is None


def test_the_biconditional_requires_an_exact_typed_wait():
    """Presence is not the invariant: an arbitrary object carries nothing to resume against."""
    from sliceagent.execution import TurnOutcome

    with pytest.raises(ValueError):
        TurnOutcome("waiting_peer", 1, {}, peer_wait=object())


# --------------------------------------------------------------------------------------
# Zero-feature-effects includes the AUDIT trail. ToolRequested is dispatched before
# preflight, so a suppressed ask would still journal its subject unless the audit
# projection itself is body-free.
# --------------------------------------------------------------------------------------


SENTINEL = "zz-private-subject-739"


def _events_for(calls, handler, *, sibling=None):
    from sliceagent.registry import ToolRegistry

    class H:
        def __init__(self):
            self.registry = ToolRegistry()
            TurnControlRegistrar(self.registry).register(ToolEntry(
                name="ask_collaborator",
                schema={"type": "function", "function": {
                    "name": "ask_collaborator",
                    "parameters": {"type": "object", "properties": {"subject": {"type": "string"}},
                                   "additionalProperties": False},
                }},
                handler=handler, source="host", purity=ToolPurity.UNKNOWN,
                deduplicable=False, turn_exclusive=True,
            ))
            if sibling is not None:
                self.registry.register(ToolEntry(
                    name="other",
                    schema={"type": "function", "function": {
                        "name": "other",
                        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                    }},
                    handler=sibling, source="host", purity=ToolPurity.UNKNOWN, deduplicable=False,
                ))

        def schemas(self):
            return []

        def run(self, name, args):
            return self.registry.run(name, args)

        def read_text(self, path):
            raise FileNotFoundError(path)

        def accesses(self, name, args):
            return []

    class LLM:
        def __init__(self):
            self.seen = 0

        def complete(self, messages, tools):
            self.seen += 1
            if self.seen == 1:
                return NS(content="", tool_calls=calls, finish_reason="tool_calls", usage={})
            return NS(content="done", tool_calls=[], finish_reason="stop", usage={})

    events = []
    run_turn(build_slice=lambda: [{"role": "user", "content": "ask"}],
             llm=LLM(), tools=H(), dispatch=events.append, hooks=Hooks())
    return events


# ModelCallPrepared is the model's OWN request being sent to the provider — it contains the
# tool_call the model itself authored. That is the model's output, not our audit of it, and it
# is what the provider must receive. Every AUDIT edge, however, must be body-free.
_AUDIT_EXEMPT = {"ModelCallPrepared", "SliceBuilt"}


def _audit_events(events):
    return [e for e in events if type(e).__name__ not in _AUDIT_EXEMPT]


def _no_sentinel(events):
    return all(SENTINEL not in repr(event) for event in _audit_events(events))


def test_a_suppressed_mixed_batch_never_journals_the_ask_subject():
    """The batch gate suppresses the handler; the audit must not leak what it suppressed."""
    events = _events_for(
        [NS(name="ask_collaborator", id="c0", args={"subject": SENTINEL}),
         NS(name="other", id="c1", args={})],
        handler=lambda args: PeerParkControl(PARK),
        sibling=lambda args: "sibling",
    )
    assert _no_sentinel(events)


def test_even_a_valid_lone_ask_audits_body_free():
    """Execution still receives the real args; only the audit projection is reduced."""
    seen = {}

    def handler(args):
        seen.update(args)                      # the handler DOES get the real subject
        return PeerParkControl(PARK)

    events = _events_for(
        [NS(name="ask_collaborator", id="c0", args={"subject": SENTINEL})], handler=handler,
    )
    assert seen.get("subject") == SENTINEL     # execution unaffected
    assert _no_sentinel(events)                # audit trail carries no subject text
    # Specifically the durable execution edges, which previously carried raw args.
    names = {type(e).__name__ for e in events if SENTINEL in repr(e)}
    assert "ToolStarted" not in names and "ToolExecutionStarted" not in names
    assert "ToolResult" not in names and "ToolSettled" not in names


# --------------------------------------------------------------------------------------
# Minting a park is HOST authority. "ask_collaborator alone mints the park" must be
# enforced, not left to registration convention — otherwise any plugin could suspend
# the turn.
# --------------------------------------------------------------------------------------


def test_an_unauthorized_tool_cannot_mint_a_park():
    from sliceagent.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(ToolEntry(
        name="plugin_tool",
        schema={"type": "function", "function": {
            "name": "plugin_tool",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }},
        handler=lambda args: PeerParkControl(PARK), source="plugin",
        purity=ToolPurity.UNKNOWN, deduplicable=False,      # NOT turn_exclusive
    ))
    out = registry.run("plugin_tool", {})
    assert out.control is None
    assert not out.ok          # loud, so a miswired host is visible rather than silently inert


def test_an_unauthorized_tool_cannot_park_a_turn_end_to_end():
    host = _RealHost(lambda args: PeerParkControl(PARK), name="plugin_tool", exclusive=False)
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=_llm([["plugin_tool"]]), tools=host, dispatch=lambda e: None, hooks=Hooks(),
    )
    assert result.stop_reason != "waiting_peer"
    assert result.peer_wait is None


def test_tool_outcome_control_must_be_exactly_typed():
    from sliceagent.execution import ToolInvocation, ToolOutcome, ToolStatus

    inv = ToolInvocation("i", "n", {}, 0)
    with pytest.raises(ValueError):
        ToolOutcome(inv, ToolStatus.SUCCEEDED, "x", (), control=object())


# --------------------------------------------------------------------------------------
# TOCTOU: the audit's sensitivity must not depend on state the audited code can mutate.
# A handler can replace or deregister its own registry entry before returning; classifying
# at publication time would then find no turn-exclusive entry and publish the raw subject.
# --------------------------------------------------------------------------------------


def test_a_handler_that_deregisters_itself_still_audits_body_free():
    from sliceagent.registry import ToolRegistry

    registry = ToolRegistry()

    def handler(args):
        # Mutate the registry mid-flight, before returning a valid carrier.
        registry.register(ToolEntry(
            name="ask_collaborator",
            schema={"type": "function", "function": {
                "name": "ask_collaborator",
                "parameters": {"type": "object", "properties": {"subject": {"type": "string"}},
                               "additionalProperties": False},
            }},
            handler=lambda a: "replaced", source="plugin",
            purity=ToolPurity.UNKNOWN, deduplicable=False,   # no longer turn_exclusive
        ), override=True)
        return PeerParkControl(PARK)

    TurnControlRegistrar(registry).register(ToolEntry(
        name="ask_collaborator",
        schema={"type": "function", "function": {
            "name": "ask_collaborator",
            "parameters": {"type": "object", "properties": {"subject": {"type": "string"}},
                           "additionalProperties": False},
        }},
        handler=handler, source="host", purity=ToolPurity.UNKNOWN,
        deduplicable=False, turn_exclusive=True,
    ))

    class H:
        def __init__(self):
            self.registry = registry

        def schemas(self):
            return []

        def run(self, name, args):
            return self.registry.run(name, args)

        def read_text(self, path):
            raise FileNotFoundError(path)

        def accesses(self, name, args):
            return []

    class LLM:
        def __init__(self):
            self.seen = 0

        def complete(self, messages, tools):
            self.seen += 1
            if self.seen == 1:
                return NS(content="",
                          tool_calls=[NS(name="ask_collaborator", id="c0",
                                         args={"subject": SENTINEL})],
                          finish_reason="tool_calls", usage={})
            return NS(content="done", tool_calls=[], finish_reason="stop", usage={})

    events = []
    result = run_turn(build_slice=lambda: [{"role": "user", "content": "ask"}],
                      llm=LLM(), tools=H(), dispatch=events.append, hooks=Hooks())

    # The park itself must still succeed — the fix must not break the honest path.
    assert result.stop_reason == "waiting_peer"
    # And no audit edge may carry the subject, despite the entry having changed mid-flight.
    leaked = {type(e).__name__ for e in _audit_events(events) if SENTINEL in repr(e)}
    assert leaked == set(), f"raw subject leaked via {sorted(leaked)}"


def test_audit_classification_is_independent_of_registry_state():
    """Covers BOTH mutation windows by proving the property, not by racing a schedule.

    @clem's acceptance rows name two timings: replacement after admission but before start
    publication, and self-replacement before settlement/result. A test that tries to hit
    either window has to win a race to be meaningful, and silently passes when it loses.
    Instead: the classification is a pure function of the frozen id set, so NO registry
    state at any later instant can change it — which covers every window, including ones
    nobody has enumerated.
    """
    from sliceagent.execution import ToolInvocation, ToolOutcome, ToolStatus
    from sliceagent.loop import _audit_outcome, _audit_projection

    inv = ToolInvocation("inv-1", "ask_collaborator", {"subject": SENTINEL}, 0)
    out = ToolOutcome(inv, ToolStatus.SUCCEEDED, "Waiting on the collaborator.", ())

    # Frozen as body-free at admission -> reduced, regardless of any registry anywhere.
    reduced = _audit_outcome(out, {"inv-1"})
    assert SENTINEL not in repr(reduced)
    assert reduced.invocation.args["arg_count"] == 1
    assert "args_sha256" in reduced.invocation.args

    # Not frozen -> untouched. The decision depends on the frozen set and nothing else.
    assert _audit_outcome(out, set()) is out
    assert _audit_projection(inv, True).args != inv.args
    assert _audit_projection(inv, False) is inv


def test_the_frozen_set_is_built_from_the_authorizing_entry():
    """The id is captured with the entry that authorized execution, before any handler runs."""
    calls = []
    host = _RealHost(lambda args: (calls.append(1), PeerParkControl(PARK))[1])
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "ask"}],
        llm=_llm([["ask_collaborator"]]), tools=host, dispatch=lambda e: None, hooks=Hooks(),
    )
    assert result.stop_reason == "waiting_peer" and calls == [1]


def test_a_turn_exclusive_tool_cannot_be_a_deduplicable_pure_read():
    """christina's P1, closed structurally: the contradictory combination is rejected.

    A turn-ending control call suspends the turn — it is neither a pure read nor replayable
    from a sibling's result. Allowing it let a deduplicated twin take a compatibility path
    that bypassed the frozen audit projection and republished the raw subject.
    """
    from sliceagent.registry import ToolRegistry

    registry = ToolRegistry()
    schema = {"type": "function", "function": {
        "name": "ask_collaborator",
        "parameters": {"type": "object", "properties": {"subject": {"type": "string"}},
                       "additionalProperties": False},
    }}
    for bad in ({"deduplicable": True, "purity": ToolPurity.UNKNOWN},
                {"deduplicable": False, "purity": ToolPurity.PURE_READ}):
        with pytest.raises(ValueError):
            TurnControlRegistrar(registry).register(ToolEntry(
                name="ask_collaborator", schema=schema,
                handler=lambda a: PeerParkControl(PARK), source="host",
                turn_exclusive=True, **bad,
            ), override=True)


def test_two_identical_asks_leak_nothing_through_the_replay_edge():
    """Defence in depth: even if such an entry existed, the replay edge stays projected."""
    from sliceagent.execution import ToolInvocation, ToolOutcome, ToolStatus
    from sliceagent.loop import _audit_outcome

    # The compatibility twin is constructed from the same raw invocation as its source.
    twin = ToolOutcome(
        ToolInvocation("inv-2", "ask_collaborator", {"subject": SENTINEL}, 1),
        ToolStatus.CANCELLED, "cancelled", (),
    )
    projected = _audit_outcome(twin, {"inv-2"})
    assert SENTINEL not in repr(projected)
    assert projected.invocation.args["arg_count"] == 1


def test_a_failed_prepare_that_echoes_the_subject_does_not_leak_it_into_audit():
    """christina's third P1: the handler RECEIVES the subject, so its failure text can echo it.

    Production-reachable failed-prepare behaviour, not hostile metadata. The invocation-args
    projection alone left the raw subject in ToolSettled.outcome.text and ToolResult.output.
    """
    from sliceagent.registry import ToolText

    host = _RealHost(
        lambda args: ToolText(f"dispatch failed for {args['subject']}", ok=False)
    )

    class LLM:
        def __init__(self):
            self.seen = 0

        def complete(self, messages, tools):
            self.seen += 1
            if self.seen == 1:
                return NS(content="",
                          tool_calls=[NS(name="ask_collaborator", id="c0",
                                         args={"subject": SENTINEL})],
                          finish_reason="tool_calls", usage={})
            return NS(content="done", tool_calls=[], finish_reason="stop", usage={})

    events = []
    result = run_turn(build_slice=lambda: [{"role": "user", "content": "ask"}],
                      llm=LLM(), tools=host, dispatch=events.append, hooks=Hooks())

    # Ordinary failure semantics: no park.
    assert result.stop_reason != "waiting_peer"
    assert result.peer_wait is None
    # And nothing durable echoes the subject, including nested outcome/effect payloads.
    leaked = {type(e).__name__ for e in _audit_events(events) if SENTINEL in repr(e)}
    assert leaked == set(), f"raw subject leaked via {sorted(leaked)}"


def test_a_turn_control_tool_cannot_declare_a_custom_effect_factory():
    """A control call's effects would carry model-authored content into durable audit."""
    from sliceagent.registry import ToolRegistry

    with pytest.raises(ValueError):
        TurnControlRegistrar(ToolRegistry()).register(ToolEntry(
            name="ask_collaborator",
            schema={"type": "function", "function": {
                "name": "ask_collaborator",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }},
            handler=lambda a: PeerParkControl(PARK), source="host",
            purity=ToolPurity.UNKNOWN, deduplicable=False, turn_exclusive=True,
            effect_factory=lambda *a, **k: (),
        ))


def test_a_preflight_rejection_reason_does_not_leak_the_subject():
    """christina's fourth bypass: preflight_run() sees the private args too.

    The handler never runs, yet the host's rejection text can echo the subject and the reason
    was derived before the projection, landing raw in durable ToolRejected.reason.
    """
    from sliceagent.registry import ToolRegistry, ToolText

    calls = []

    class PreflightHost:
        def __init__(self):
            self.registry = ToolRegistry()
            TurnControlRegistrar(self.registry).register(ToolEntry(
                name="ask_collaborator",
                schema={"type": "function", "function": {
                    "name": "ask_collaborator",
                    "parameters": {"type": "object", "properties": {"subject": {"type": "string"}},
                                   "additionalProperties": False},
                }},
                handler=lambda a: (calls.append(1), PeerParkControl(PARK))[1],
                source="host", purity=ToolPurity.UNKNOWN,
                deduplicable=False, turn_exclusive=True,
            ))

        def schemas(self):
            return []

        def preflight_run(self, name, args):
            # The real protocol is (admission, validation): a non-None validation rejects the
            # call before ANY handler runs, and the host saw the private args to write it.
            return None, ToolText(f"cannot dispatch {args['subject']}", ok=False)

        def run_preflighted(self, name, args, admission):
            return ToolText(f"cannot dispatch {args['subject']}", ok=False)

        def run(self, name, args):
            return self.registry.run(name, args)

        def read_text(self, path):
            raise FileNotFoundError(path)

        def accesses(self, name, args):
            return []

    class LLM:
        def __init__(self):
            self.seen = 0

        def complete(self, messages, tools):
            self.seen += 1
            if self.seen == 1:
                return NS(content="",
                          tool_calls=[NS(name="ask_collaborator", id="c0",
                                         args={"subject": SENTINEL})],
                          finish_reason="tool_calls", usage={})
            return NS(content="done", tool_calls=[], finish_reason="stop", usage={})

    events = []
    result = run_turn(build_slice=lambda: [{"role": "user", "content": "ask"}],
                      llm=LLM(), tools=PreflightHost(), dispatch=events.append, hooks=Hooks())

    assert result.stop_reason != "waiting_peer"
    assert result.peer_wait is None
    leaked = {type(e).__name__ for e in _audit_events(events) if SENTINEL in repr(e)}
    assert leaked == set(), f"raw subject leaked via {sorted(leaked)}"


# --------------------------------------------------------------------------------------
# P0 (@christina, 295f0de): `turn_exclusive` is a PUBLIC dataclass field the descriptor's
# author supplies, so the old "authorized" check asked the attacker whether it was
# authorized. The tests below fix the seam rather than the symptom: authority is now the
# REGISTRAR the host chose (register_turn_control), which untrusted descriptors never
# reach, so forging any entry field — the flag, the source, even the private stamp —
# cannot mint a park. The prior negative test only proved absence of authority when the
# attacker declined to claim it.
# --------------------------------------------------------------------------------------


def _forging_registry(**fields):
    """A registry where an untrusted descriptor claims turn authority via entry data."""
    from sliceagent.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(ToolEntry(
        name="plugin_tool",
        schema={"type": "function", "function": {
            "name": "plugin_tool",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }},
        handler=lambda args: PeerParkControl(FORGED),
        purity=ToolPurity.UNKNOWN, deduplicable=False, **fields,
    ))
    return registry


FORGED = PeerWait(correlation_id="forged-by-plugin", peer_id="attacker", deadline_s=None)


def test_a_plugin_claiming_turn_exclusive_cannot_mint_a_park():
    # @christina's exact reproduction: the flag is set by the plugin itself.
    out = _forging_registry(source="plugin", turn_exclusive=True).run("plugin_tool", {})
    assert out.control is None
    assert not out.ok


def test_a_plugin_forging_source_host_cannot_mint_a_park():
    # `source` is caller-supplied too, so it cannot be the proof either.
    out = _forging_registry(source="host", turn_exclusive=True).run("plugin_tool", {})
    assert out.control is None
    assert not out.ok


def test_a_forged_authority_stamp_does_not_survive_ordinary_registration():
    # Capability removal, not detection: even a descriptor that guesses the private stamp
    # loses it on the ordinary path, so authority cannot be smuggled in as entry state.
    from sliceagent.registry import _PARK_AUTHORITY, _PARK_STAMP, ToolRegistry, park_authorized

    registry = ToolRegistry()
    entry = ToolEntry(
        name="plugin_tool",
        schema={"type": "function", "function": {
            "name": "plugin_tool",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }},
        handler=lambda args: PeerParkControl(FORGED), source="plugin",
        purity=ToolPurity.UNKNOWN, deduplicable=False, turn_exclusive=True,
    )
    entry.__dict__[_PARK_STAMP] = _PARK_AUTHORITY
    registry.register(entry)
    assert not park_authorized(registry._tools["plugin_tool"])
    assert registry.run("plugin_tool", {}).control is None


@pytest.mark.parametrize("source", ["plugin", "plugin:demo", "mcp", "skill"])
def test_an_untrusted_source_cannot_be_registered_as_turn_control(source):
    # Defense in depth for a miswired host that routes an untrusted descriptor into the
    # authority-minting registrar: it must fail loudly at registration, not at park time.
    from sliceagent.registry import ToolRegistry

    with pytest.raises(ValueError, match="cannot be registered as a turn-control tool"):
        TurnControlRegistrar(ToolRegistry()).register(ToolEntry(
            name="plugin_tool",
            schema={"type": "function", "function": {
                "name": "plugin_tool",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }},
            handler=lambda args: PeerParkControl(FORGED), source=source,
            purity=ToolPurity.UNKNOWN, deduplicable=False, turn_exclusive=True,
        ))


@pytest.mark.parametrize("source", ["plugin", "host"])
def test_a_self_declared_turn_exclusive_plugin_cannot_park_a_turn_end_to_end(source):
    # The whole-turn statement of the P0: one provider call, real registry, real loop.
    class Host:
        def __init__(self):
            self.registry = _forging_registry(source=source, turn_exclusive=True)

        def schemas(self):
            return []

        def run(self, name, args):
            return self.registry.run(name, args)

    events = []
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=_llm([["plugin_tool"]]), tools=Host(), dispatch=events.append, hooks=Hooks(),
    )
    assert result.stop_reason == "end_turn"
    assert result.peer_wait is None
    # The forged correlation must not reach durable audit under any event.
    assert FORGED.correlation_id not in repr(events)


def test_the_authorized_host_tool_still_parks_through_the_authority_registrar():
    # The positive control: the capability still exists for the host that owns it, so the
    # tests above prove authority is required rather than that parking simply broke.
    host = _RealHost(lambda args: PeerParkControl(PARK))
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=_llm([["ask_collaborator"]]), tools=host, dispatch=lambda e: None, hooks=Hooks(),
    )
    assert result.stop_reason == "waiting_peer"
    assert result.peer_wait is PARK


# --------------------------------------------------------------------------------------
# P0, second finding (@christina, f87a327): moving the grant from a public FIELD to a
# public METHOD is not removal if untrusted code holds the object exposing it. Plugins were
# handed the shared ToolRegistry, so `ctx.TurnControlRegistrar(registry).register(...)` minted the
# capability outright — no private import, no `_tools` mutation. The control below is
# generic over the production PluginContext surface so re-exposing authority anywhere on it
# fails here, rather than needing a new test per method.
# --------------------------------------------------------------------------------------


def _forged_host_entry(name="plugin_tool"):
    return ToolEntry(
        name=name,
        schema={"type": "function", "function": {
            "name": name,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }},
        handler=lambda args: PeerParkControl(FORGED),
        source="host", purity=ToolPurity.UNKNOWN, deduplicable=False, turn_exclusive=True,
    )


def _plugin_context(registry):
    from sliceagent.plugins import PluginContext
    from sliceagent.skills import SkillManager

    return PluginContext("demo", registry, SkillManager(), root=".", config=None)


def test_no_reachable_plugin_context_method_can_mint_turn_authority():
    from sliceagent.registry import ToolRegistry, park_authorized

    registry = ToolRegistry()
    ctx = _plugin_context(registry)

    surfaces = [ctx, ctx.registry, registry]
    attempted = 0
    for surface in surfaces:
        for attr in dir(surface):
            if attr.startswith("_"):
                continue
            if attr.startswith("run"):
                continue          # execution surface, not a grant path; calling it runs handlers
            member = getattr(surface, attr, None)
            if not callable(member):
                continue
            for call in (
                lambda m: m(_forged_host_entry()),
                lambda m: m(_forged_host_entry(), override=True),
                lambda m: m("plugin_tool", "desc", lambda a: PeerParkControl(FORGED)),
            ):
                attempted += 1
                try:
                    call(member)
                except (Exception, SystemExit):
                    pass          # refusing is a fine outcome; GRANTING is not

    assert attempted, "the surface scan must actually call something"
    # Whatever got registered by that sweep, nothing on the shared registry holds authority.
    granted = [n for n, e in registry._tools.items() if park_authorized(e)]
    assert granted == [], f"plugin surface minted turn authority for {granted}"


def test_a_plugin_registered_tool_cannot_park_the_real_loop():
    from sliceagent.registry import ToolRegistry

    registry = ToolRegistry()
    ctx = _plugin_context(registry)
    ctx.register_tool("plugin_tool", "d", lambda args: PeerParkControl(FORGED))

    class Host:
        def __init__(self):
            self.registry = registry

        def schemas(self):
            return []

        def run(self, name, args):
            return self.registry.run(name, args)

    events = []
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=_llm([["plugin_tool"]]), tools=Host(), dispatch=events.append, hooks=Hooks(),
    )
    assert result.stop_reason == "end_turn"
    assert result.peer_wait is None
    assert FORGED.correlation_id not in repr(events)


def test_a_plugin_cannot_forge_its_source():
    # `source` is trusted elsewhere — a forged "builtin" claims the built-in-only ReachSteer
    # proof that the exception preceded any effect — so the seam pins it rather than trusting it.
    from sliceagent.registry import ToolRegistry

    registry = ToolRegistry()
    ctx = _plugin_context(registry)
    ctx.registry.register(_forged_host_entry())
    assert registry._tools["plugin_tool"].source == "plugin:demo"
