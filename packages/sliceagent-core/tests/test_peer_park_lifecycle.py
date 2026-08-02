"""Regressions for the peer-park lifecycle defects found in the owner's merge review.

Every guard these pin was previously UNPINNED: the whole C4 wrong-peer + elapsed_s fix could
be deleted with the conformance probe still 3/3 and the suite still green. These tests are the
mutation controls — remove a production guard and one of them must go red.
"""
from __future__ import annotations

import pytest

from sliceagent_core.active_work import GraphValidationError, OutputRef, WorkGraph
from sliceagent_core.interfaces import (
    PeerDelegation,
    PeerResult,
    PeerWait,
    correlate_peer_result,
)

PARK = PeerWait(correlation_id="review-1", peer_id="reviewer", deadline_s=30.0)


def parked_graph() -> WorkGraph:
    return WorkGraph().open_request("evt-1", "do the thing").seal_current(
        "waiting_peer", peer_wait=PARK
    )


# --------------------------------------------------------------------------------------
# Defect 1: the park must survive a re-seal instead of raising or vanishing.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("stop_reason", ["aborted", "error", "interrupted", "end_turn"])
def test_resealing_a_parked_root_preserves_the_park(stop_reason):
    """Re-sealing used to raise GraphValidationError for every one of these.

    The raise escaped `_seal_local_turn` (cli.py has no try/except there), bypassing the
    designed TurnCommitted(ok=False) lane and killing the turn commit outright.
    """
    root = parked_graph().seal_current(stop_reason).request_roots[-1]
    assert root.status == "waiting_peer"
    assert root.peer_wait == PARK


def test_delivering_a_response_does_not_silently_destroy_the_park():
    """The silent variant: status became `delivered` with peer_wait dropped.

    The park disappeared with no typed record, so the peer's eventual reply could never land.
    """
    root = parked_graph().seal_current(
        "end_turn", OutputRef(kind="message", ref="resp-1")
    ).request_roots[-1]
    assert root.status == "waiting_peer"
    assert root.peer_wait == PARK


def test_resolving_the_park_must_be_explicit():
    """Leaving the park is legal, but only as a deliberate typed act."""
    root = parked_graph().seal_current(
        "end_turn", OutputRef(kind="message", ref="resp-1"), resolve_peer_wait=True
    ).request_roots[-1]
    assert root.status == "delivered"
    assert root.peer_wait is None


def test_sealing_waiting_peer_without_typed_state_is_refused():
    """`waiting_peer` may never exist without its correlation state."""
    graph = WorkGraph().open_request("evt-1", "do the thing")
    with pytest.raises(GraphValidationError):
        graph.seal_current("waiting_peer")


def test_a_new_park_replaces_the_carried_one():
    replacement = PeerWait(correlation_id="review-2", peer_id="other", deadline_s=5.0)
    root = parked_graph().seal_current(
        "waiting_peer", peer_wait=replacement
    ).request_roots[-1]
    assert root.peer_wait == replacement


# --------------------------------------------------------------------------------------
# Defect 4: the C4 authority guards must be load-bearing, not decorative.
# --------------------------------------------------------------------------------------


DELEGATION = PeerDelegation(
    correlation_id="delegate-9", peer_id="worker", task="inspect shard B", deadline_s=20.0
)


def _result(**kw):
    base = dict(correlation_id="delegate-9", peer_id="worker", status="ok", report="done")
    base.update(kw)
    return PeerResult(**base)


def test_matching_result_is_accepted():
    assert correlate_peer_result(DELEGATION, _result()) is not None


def test_wrong_peer_is_refused():
    """Deleting this gate previously left conformance 3/3 and the suite green."""
    assert correlate_peer_result(DELEGATION, _result(peer_id="impostor")) is None


def test_wrong_correlation_is_refused():
    assert correlate_peer_result(DELEGATION, _result(correlation_id="delegate-other")) is None


@pytest.mark.parametrize("elapsed", [float("nan"), float("inf"), -1.0, -0.0001])
def test_hostile_elapsed_values_are_refused(elapsed):
    """NaN defeats every `>` comparison, so an expired result would resurrect silently."""
    assert correlate_peer_result(DELEGATION, _result(), elapsed_s=elapsed) is None


def test_expired_result_is_refused():
    assert correlate_peer_result(DELEGATION, _result(), elapsed_s=20.0001) is None


def test_deadline_boundary_is_inclusive():
    assert correlate_peer_result(DELEGATION, _result(), elapsed_s=20.0) is not None


def test_non_numeric_elapsed_raises_typed_error():
    with pytest.raises(ValueError):
        correlate_peer_result(DELEGATION, _result(), elapsed_s="20")


# --------------------------------------------------------------------------------------
# The reaper: a park nobody can expire is a permanent trap. `deadline_s` was validated and
# serialized but never compared to anything before this.
# --------------------------------------------------------------------------------------


from sliceagent_core.active_work import expire_peer_waits  # noqa: E402


def test_an_overdue_park_is_expired_back_to_live_work():
    graph, expired = expire_peer_waits(parked_graph(), {"review-1": 30.0001})
    root = graph.request_roots[-1]
    assert expired == ("review-1",)
    assert root.status == "in_progress"
    assert root.peer_wait is None
    assert root.stop_reason == "peer_wait_expired"


def test_a_park_within_its_deadline_is_untouched():
    graph, expired = expire_peer_waits(parked_graph(), {"review-1": 29.9})
    assert expired == ()
    assert graph.request_roots[-1].status == "waiting_peer"


def test_the_deadline_boundary_is_inclusive():
    """Matches correlate_peer_result: elapsed == deadline is still inside the window."""
    _, expired = expire_peer_waits(parked_graph(), {"review-1": 30.0})
    assert expired == ()


def test_an_unknown_correlation_never_expires_someone_elses_park():
    _, expired = expire_peer_waits(parked_graph(), {"some-other-park": 9_999.0})
    assert expired == ()


def test_an_unbounded_park_never_expires_by_time():
    from sliceagent_core.interfaces import PeerWait as _PW

    graph = WorkGraph().open_request("evt-1", "x").seal_current(
        "waiting_peer", peer_wait=_PW(correlation_id="c", peer_id="p", deadline_s=None)
    )
    _, expired = expire_peer_waits(graph, {"c": 10_000.0})
    assert expired == ()


@pytest.mark.parametrize("elapsed", [float("nan"), float("inf"), -1.0])
def test_hostile_elapsed_readings_are_refused(elapsed):
    """A NaN elapsed defeats every `>` comparison and would keep a park alive forever."""
    with pytest.raises(Exception):
        expire_peer_waits(parked_graph(), {"review-1": elapsed})


def test_a_graph_with_no_park_is_returned_unchanged():
    graph = WorkGraph().open_request("evt-1", "x")
    same, expired = expire_peer_waits(graph, {"review-1": 9_999.0})
    assert expired == ()
    assert same is graph


def test_a_workspace_transition_does_not_destroy_the_park():
    """linglong's find: the CLI passes transitioned=True for workspace handoff.

    An earlier ordering checked `transitioned` before park preservation, so a handoff
    silently produced in_progress + peer_wait=None with resolve_peer_wait=False.
    """
    root = parked_graph().seal_current(
        "workspace_transition", transitioned=True
    ).request_roots[-1]
    assert root.status == "waiting_peer"
    assert root.peer_wait == PARK


def test_a_workspace_transition_can_still_resolve_the_park_explicitly():
    root = parked_graph().seal_current(
        "workspace_transition", transitioned=True, resolve_peer_wait=True
    ).request_roots[-1]
    assert root.status == "in_progress"
    assert root.peer_wait is None


@pytest.mark.parametrize("truthy", ["yes", 1, [0]])
def test_non_bool_resolve_peer_wait_is_refused(truthy):
    """An authority flag must be an exact bool, not anything truthy."""
    with pytest.raises(GraphValidationError):
        parked_graph().seal_current("end_turn", resolve_peer_wait=truthy)


def test_a_park_survives_a_user_wait_by_default():
    """Explicit disposition of the parked -> waiting_user finding.

    Default: the durable park wins; the segment ending on a user wait does not discard it.
    """
    root = parked_graph().seal_current("waiting_user").request_roots[-1]
    assert root.status == "waiting_peer"
    assert root.peer_wait == PARK


def test_an_explicitly_resolved_park_may_hand_off_to_a_user_wait():
    """The transition table omitted waiting_peer -> waiting_user, so this used to raise.

    The two wait axes must be able to hand off: a resolved peer park can legitimately end
    the segment waiting on the user instead.
    """
    root = parked_graph().seal_current(
        "waiting_user", resolve_peer_wait=True
    ).request_roots[-1]
    assert root.status == "waiting_user"
    assert root.peer_wait is None
