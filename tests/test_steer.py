"""Steer: user input typed mid-turn lands at step boundaries as a plain user message,
in the SAME conversation and the SAME turn — the in-flight model call is never aborted."""
from __future__ import annotations

import queue
from types import SimpleNamespace as NS

from sliceagent.events import AssistantText, SteerDelivered, TurnEnd, TurnPhaseChanged
from sliceagent.hooks import Hooks
from sliceagent.loop import run_turn
from sliceagent.registry import ToolText


def _call(name: str, call_id: str, **args):
    return NS(name=name, id=call_id, args=args)


def _tool_response(call):
    return NS(content="", tool_calls=[call], finish_reason="tool_calls", usage={})


def _done_response(text="done"):
    return NS(content=text, tool_calls=[], finish_reason="stop", usage={})


class _ScriptLLM:
    def __init__(self, responses, on_call=None):
        self.responses = list(responses)
        self.seen = []
        self.on_call = on_call

    def complete(self, messages, _schemas):
        self.seen.append([dict(message) for message in messages])
        if self.on_call is not None:
            self.on_call(len(self.seen))
        return self.responses.pop(0)


class _Host:
    def schemas(self):
        return []

    def accesses(self, _name, _args):
        return []

    def run(self, name, args):
        return ToolText("observation")


def test_steer_lands_in_next_provider_call_after_tool_results():
    q: queue.Queue = queue.Queue()
    llm = _ScriptLLM(
        [_tool_response(_call("read_file", "c1", path="a.py")), _done_response()],
        on_call=lambda n: q.put("focus on the parser") if n == 1 else None,   # types during call 1
    )
    events = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "inspect"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=Hooks(), steer_queue=q,
    )
    assert outcome.stop_reason == "end_turn"
    assert len(llm.seen) == 2
    second = llm.seen[1]
    # sequence validity: the steer follows the tool result, as a plain user-role message
    assert second[-1] == {"role": "user", "content": "focus on the parser"}
    assert second[-2]["role"] == "tool" and second[-2]["tool_call_id"].startswith("c1")
    steers = [e for e in events if isinstance(e, SteerDelivered)]
    assert [e.content for e in steers] == ["focus on the parser"]


def test_last_second_steer_keeps_the_turn_alive():
    q: queue.Queue = queue.Queue()
    llm = _ScriptLLM(
        [_done_response("final answer"), _done_response("follow-up")],
        on_call=lambda n: q.put("wait, one more thing") if n == 1 else None,
    )
    events = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "do it"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=Hooks(), steer_queue=q,
    )
    assert outcome.stop_reason == "end_turn"
    assert len(llm.seen) == 2, "a steer arriving as the model finishes must force another step"
    second = llm.seen[1]
    assert second[-1] == {"role": "user", "content": "wait, one more thing"}
    assert second[-2]["role"] == "assistant" and second[-2]["content"] == "final answer"
    assert any(isinstance(e, TurnEnd) for e in events), "the turn still seals cleanly afterwards"


def test_no_steer_queue_means_clean_single_pass():
    llm = _ScriptLLM([_done_response()])
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "hi"}],
        llm=llm, tools=_Host(), dispatch=lambda _e: None, hooks=Hooks(),
    )
    assert outcome.stop_reason == "end_turn" and len(llm.seen) == 1


def test_budget_park_never_acks_undelivered_steers():
    """#49 T1-1: a steer queued when a resource gate parks the turn must NOT get a SteerDelivered
    receipt — the model never saw it. It returns unacked on leftover_steers for the next turn."""
    class _Ceiling(Hooks):
        def before_step(self, step):
            return {"stop_turn": True, "reason": "over budget"}

    q: queue.Queue = queue.Queue()
    q.put(("too late", "adm-9"))
    llm = _ScriptLLM([])                     # the model must never be called
    events = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=_Ceiling(), steer_queue=q,
    )
    assert outcome.stop_reason == "token_budget"
    assert llm.seen == [], "no model call happened, so nothing can count as delivered"
    assert not [e for e in events if isinstance(e, SteerDelivered)], \
        "a park must never mint a false delivery receipt"
    assert outcome.leftover_steers == (("too late", "adm-9"),)


def test_retirement_window_steer_returns_unacked_on_leftovers():
    """#49 T1-2: a steer landing between the loop's final drain and turn retirement was silently
    stranded. It must come back on leftover_steers (unacked) while the turn still seals cleanly."""
    q: queue.Queue = queue.Queue()

    class _LateTyper(Hooks):
        def should_continue_after_stop(self, stop_reason):
            q.put(("typed during finalization", "adm-late"))   # deterministic: inside the window
            return None

    llm = _ScriptLLM([_done_response("answer")])
    events = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=_LateTyper(), steer_queue=q,
    )
    assert outcome.stop_reason == "end_turn"
    assert any(isinstance(e, TurnEnd) for e in events)
    assert not [e for e in events if isinstance(e, SteerDelivered)]
    assert outcome.leftover_steers == (("typed during finalization", "adm-late"),)
    assert q.empty(), "the kernel sweep owns the window; nothing is left dangling in the queue"


def test_steered_final_candidate_is_observed_not_hidden():
    """#49 T1-3: when a steer keeps the turn alive past a composed 'final' answer, that answer
    stays in the trajectory and influences later calls — so it must be dispatched (non-final),
    never silent hidden state."""
    q: queue.Queue = queue.Queue()
    llm = _ScriptLLM(
        [_done_response("FIRST ANSWER"), _done_response("SECOND ANSWER")],
        on_call=lambda n: q.put("keep going") if n == 1 else None,
    )
    events = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=Hooks(), steer_queue=q,
    )
    assert outcome.stop_reason == "end_turn"
    texts = [(e.content, e.final) for e in events if isinstance(e, AssistantText)]
    assert ("FIRST ANSWER", False) in texts, \
        "the superseded terminal candidate must be observable, not hidden model state"
    assert texts[-1] == ("SECOND ANSWER", True)


def test_broken_steer_queue_surfaces_and_never_kills_the_turn():
    """#49 T1-4: a raising queue is a broken steering channel — surface it once as a typed phase
    event and finish the turn; silence (the old behavior) masked dead steering from the host."""
    class _BrokenQueue:
        def get_nowait(self):
            raise RuntimeError("durable queue offline")

    llm = _ScriptLLM([_done_response("done")])
    events = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=Hooks(), steer_queue=_BrokenQueue(),
    )
    assert outcome.stop_reason == "end_turn"
    broken = [e for e in events if isinstance(e, TurnPhaseChanged) and e.phase == "steer_channel_broken"]
    assert len(broken) == 1, "exactly one typed surfacing of the broken channel"
    assert outcome.leftover_steers == ()


def test_steer_admission_id_rides_the_delivery_receipt():
    """A durable host pairs deliveries with its inbox: (text, admission_id) items keep the id,
    equal-text steers stay distinguishable, and the trajectory carries only the text."""
    q: queue.Queue = queue.Queue()
    q.put(("same words", "adm-1"))
    q.put(("same words", "adm-2"))
    llm = _ScriptLLM([_done_response("ok"), _done_response("done")])
    events = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=Hooks(), steer_queue=q,
    )
    assert outcome.stop_reason == "end_turn"
    receipts = [e for e in events if isinstance(e, SteerDelivered)]
    assert [e.admission_id for e in receipts] == ["adm-1", "adm-2"]
    assert all(e.content == "same words" for e in receipts)
    first = llm.seen[0]   # queued pre-turn → drained at the top of step 1
    steered = [m for m in first if m["role"] == "user" and m["content"] == "same words"]
    assert len(steered) == 2, "both steers land in the trajectory as plain user text"
def test_prepend_leftover_steers_restores_ingress_order_as_is():
    """The TUI handback restores swept leftovers AHEAD of turn-time arrivals (they are older), and
    every item shape passes through unstringified — (text, admission_id) pairs and typed peer
    messages intact. unfinished_tasks is NOT re-counted: the sweep get()-ed the items without
    task_done(), so they retain their original unfinished ownership and join() stays completable."""
    from sliceagent.cli import _prepend_leftover_steers
    from sliceagent.interfaces import PeerMessage

    peer = PeerMessage(message_id="m-1", peer_id="peer-a", content="review ready")
    q: queue.Queue = queue.Queue()
    q.put(("A", "adm-1"))
    q.put(peer)
    swept = (q.get_nowait(), q.get_nowait())     # the core sweep: get() leaves ownership outstanding
    q.put("B")                                   # typed DURING the turn, after the sweep
    outstanding = q.unfinished_tasks
    _prepend_leftover_steers(q, swept)
    assert q.unfinished_tasks == outstanding, "swept items must not be re-counted (linglong)"
    assert q.get_nowait() == ("A", "adm-1")
    assert q.get_nowait() is peer
    assert q.get_nowait() == "B"
    for _ in range(3):
        q.task_done()
    assert q.unfinished_tasks == 0, "one task_done per redriven item drains the counter to zero"


def test_prepend_leftover_steers_is_atomic_against_concurrent_put():
    """linglong's [C, A, B] race: drain-then-restore released the queue between the drain and the
    restore, so a steer enqueued mid-restore landed AHEAD of the swept prefix. The prepend is one
    critical section, so the prefix can never be split or overtaken — final order is deterministic."""
    import threading

    from sliceagent.cli import _prepend_leftover_steers

    q: queue.Queue = queue.Queue()
    q.put("A1")
    q.put("A2")
    swept = (q.get_nowait(), q.get_nowait())
    q.put("B")                                   # arrived during the turn, after the sweep
    with q.mutex:                                # force both contenders to pend until we release
        prepender = threading.Thread(target=_prepend_leftover_steers, args=(q, swept))
        producer = threading.Thread(target=q.put, args=("C",))   # the still-newer concurrent steer
        prepender.start()
        producer.start()
    prepender.join()
    producer.join()
    assert [q.get_nowait() for _ in range(4)] == ["A1", "A2", "B", "C"]


def test_terminal_handback_splits_user_prose_from_typed_items():
    """clem's finding: the terminal handback joined leftover items into a draft with "\\n".join —
    a TypeError on any typed item, and user-authority forgery had it coerced. Only plain user text
    becomes a draft, classified EXACT-SHAPE (raw str or (str, "")): a non-string first element or
    a blank/non-string admission id stays typed, never coerced into end-user prose (linglong)."""
    from sliceagent.interfaces import PeerMessage
    from sliceagent.loop import _split_steer_handback

    peer = PeerMessage(message_id="m-2", peer_id="peer-b", content="ship it")
    draft, typed = _split_steer_handback([
        "keep typing", ("note", ""), ["list pair", ""],      # exact user-prose shapes → draft
        ("inbox item", "adm-9"), peer,                        # durable pair + peer message → typed
        ("   ", ""),                                          # blank text → dropped entirely
        ("blank adm", " "), (None, ""), ("text", None),       # non-exact shapes → typed, not coerced
    ])
    assert draft == ["keep typing", "note", "list pair"]
    assert typed == [("inbox item", "adm-9"), peer, ("blank adm", " "), (None, ""), ("text", None)]


def test_step_drain_rejects_malformed_items_without_coercion():
    """clem's end-to-end authority finding: a malformed pair like (PeerMessage, "") or
    ("text", None) used to be str()-coerced into a user-role message with a SteerDelivered
    receipt — peer input forged as END-USER authority. The queue boundary is now exact-shape:
    malformed items get a typed SteerRejected and never reach the trajectory."""
    from sliceagent.events import PeerMessageDelivered, SteerRejected
    from sliceagent.interfaces import PeerMessage

    peer = PeerMessage(message_id="m-9", peer_id="peer-x", content="peer says ship")
    disguised = (peer, "")
    bad_admission = ("text", None)
    hostile = {"hostile": "dict"}
    q: queue.Queue = queue.Queue()
    q.put("real user steer")
    q.put(disguised)             # malformed pair — must NOT become str(peer) user text
    q.put(bad_admission)         # non-string admission id — must NOT coerce to ("text", "")
    q.put(hostile)
    llm = _ScriptLLM([_done_response("ok"), _done_response("done")])
    events = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=Hooks(), steer_queue=q,
    )
    assert outcome.stop_reason == "end_turn"
    delivered = [e for e in events if isinstance(e, SteerDelivered)]
    assert [e.content for e in delivered] == ["real user steer"], "only the legal steer is delivered"
    assert not [e for e in events if isinstance(e, PeerMessageDelivered)], \
        "a malformed pair must not reach the peer lane either"
    rejected = [e.shape for e in events if isinstance(e, SteerRejected)]
    assert rejected == ["pair(PeerMessage,str)", "pair(str,NoneType)", "dict"], rejected
    first = llm.seen[0]   # queued pre-turn → drained at the top of step 1
    users = [m["content"] for m in first if m["role"] == "user"]
    assert "real user steer" in users
    assert not any("PeerMessage(" in c or "hostile" in c for c in users), \
        "no coerced object repr lands in the provider trajectory"
    # Rejection is not disappearance: ownership transfers INTACT to leftover_steers (drain order)
    # so a durable host can still reconcile the malformed admissions.
    assert list(outcome.leftover_steers) == [disguised, bad_admission, hostile]
    assert outcome.leftover_steers[0] is disguised


def test_retirement_sweep_preserves_malformed_items_as_is():
    """The retirement sweep must return malformed items AS-IS (host-owned), never str()-coerced —
    the caller's typed lane redrives them and the next step drain rejects them typed."""
    from sliceagent.interfaces import PeerMessage

    peer = PeerMessage(message_id="m-10", peer_id="peer-y", content="resume please")
    malformed = (peer, "")
    q: queue.Queue = queue.Queue()

    class _LateTyper(Hooks):
        def should_continue_after_stop(self, stop_reason):
            q.put(malformed)       # lands in the retirement window, after the final drain
            q.put(("text", None))
            return None

    llm = _ScriptLLM([_done_response("answer")])
    events = []
    outcome = run_turn(
        build_slice=lambda: [{"role": "user", "content": "go"}],
        llm=llm, tools=_Host(), dispatch=events.append, hooks=_LateTyper(), steer_queue=q,
    )
    assert outcome.stop_reason == "end_turn"
    assert outcome.leftover_steers[0] is malformed, "malformed pair preserved intact, not stringified"
    assert outcome.leftover_steers[1] == ("text", None)
    assert not [e for e in events if isinstance(e, SteerDelivered)]
