from __future__ import annotations

from sliceagent.events import ToolResult
from sliceagent.execution import ToolInvocation, ToolStatus
from sliceagent.pfc import Slice, record_user, slice_sink
from sliceagent.tools import LocalToolHost


def test_active_work_helpers_are_core_owned_with_legacy_tool_aliases():
    from sliceagent import tools
    from sliceagent_core import active_work

    assert tools.build_work_delta is active_work.build_work_delta
    assert tools._plan_progress_payload is active_work._plan_progress_payload
    assert tools._MODEL_WORK_STATUSES is active_work._MODEL_WORK_STATUSES


def prepared():
    state = Slice(); state.reset("compound task")
    record_user(
        state, "switch, inspect, and report", source_event_id="event-1",
        logical_id="logical-1", workspace_epoch=2,
    )
    host = LocalToolHost()
    host.bind_active_work(lambda: (state.active_work, "logical-1", 2))
    return state, host


def invoke(host, args, call_id="call-1"):
    return host.registry.invoke(ToolInvocation(
        id=call_id, name="update_work", args=args, provider_index=0,
    ))


def reduce(state, outcome):
    slice_sink(state)(ToolResult(
        name="update_work", args=dict(outcome.invocation.args), output=outcome.text,
        failing=outcome.failing, status=outcome.status.value,
        invocation_id=outcome.invocation.id, outcome=outcome,
    ))


def test_update_work_creates_typed_source_linked_child_and_replays_exactly_once():
    state, host = prepared()
    outcome = invoke(host, {
        "expected_revision": 1,
        "changes": [{
            "id": "inspect-target", "description": "Inspect the target architecture",
            "status": "in_progress",
            "add_resources": [{"kind": "workspace_file", "ref": "src/app.py"}],
        }],
    })
    assert outcome.status is ToolStatus.SUCCEEDED
    assert outcome.effects[0].kind == "work_delta"
    reduce(state, outcome)
    child = state.active_work.get("inspect-target")
    assert child is not None and child.root_id == state.active_work.request_roots[0].id
    assert child.source_refs == state.active_work.request_roots[0].source_refs
    assert child.resource_refs[0].workspace_epoch == 2
    revision = state.active_work.revision

    reduce(state, outcome)
    assert state.active_work.revision == revision


def test_work_delta_effect_carries_complete_plan_progress_projection():
    state, host = prepared()
    host._verify_runner = lambda _command: (True, "ok")
    created = invoke(host, {
        "expected_revision": 1,
        "changes": [
            {
                "id": "implement", "description": "Implement the change", "status": "in_progress",
                "verify": ["focused-check"], "done_when": "focused check passes",
            },
            {"id": "document", "description": "Document the behavior", "status": "open"},
        ],
    })
    assert created.status is ToolStatus.SUCCEEDED
    projection = created.effects[0].payload["plan_progress"]
    assert projection == {
        "total": 2, "done": 0, "current": "Implement the change", "current_index": 1,
        "items": [
            {
                "id": "implement", "status": "in_progress", "description": "Implement the change",
                "done_when": "focused check passes", "host_verified": False,
            },
            {
                "id": "document", "status": "open", "description": "Document the behavior",
                "done_when": "", "host_verified": False,
            },
        ],
    }
    reduce(state, created)

    completed = invoke(host, {
        "expected_revision": 2,
        "changes": [{"id": "implement", "status": "ready"}],
    }, "complete")
    assert completed.status is ToolStatus.SUCCEEDED
    projection = completed.effects[0].payload["plan_progress"]
    assert projection == {
        "total": 2, "done": 1, "current": "Document the behavior", "current_index": 2,
        "items": [
            {
                "id": "implement", "status": "verified", "description": "Implement the change",
                "done_when": "focused check passes", "host_verified": True,
            },
            {
                "id": "document", "status": "open", "description": "Document the behavior",
                "done_when": "", "host_verified": False,
            },
        ],
    }


def test_update_work_rejects_terminal_forgery_root_mutation_and_stale_revision():
    state, host = prepared()
    terminal = invoke(host, {
        "expected_revision": state.active_work.revision,
        "changes": [{"id": "fake", "description": "fake", "status": "verified"}],
    }, "terminal")
    assert terminal.status is ToolStatus.FAILED
    assert "cannot set delivered/verified" in terminal.text

    root_id = state.active_work.request_roots[0].id
    mutate_root = invoke(host, {
        "expected_revision": state.active_work.revision,
        "changes": [{"id": root_id, "description": "rewrite user root", "status": "cancelled"}],
    }, "root")
    assert mutate_root.status is ToolStatus.FAILED
    assert "current root is host-owned" in mutate_root.text

    stale = invoke(host, {
        "expected_revision": 0,
        "changes": [{"id": "child", "description": "child"}],
    }, "stale")
    assert stale.status is ToolStatus.FAILED
    assert "expected revision 0" in stale.text


def test_current_correction_can_explicitly_supersede_an_older_open_request_root():
    state, host = prepared()
    older = state.active_work.request_roots[0]
    record_user(
        state, "instead, only write the report", source_event_id="event-2",
        logical_id="logical-2", workspace_epoch=2,
    )
    current = state.active_work.request_roots[-1]
    host.bind_active_work(lambda: (state.active_work, "logical-2", 2))
    outcome = invoke(host, {
        "expected_revision": state.active_work.revision,
        "changes": [{
            "id": older.id, "status": "superseded", "superseded_by": current.id,
        }],
    })
    assert outcome.status is ToolStatus.SUCCEEDED
    reduce(state, outcome)
    assert state.active_work.get(older.id).status == "superseded"
    assert state.active_work.get(older.id).superseded_by == current.id


def test_update_work_ready_is_a_nonterminal_claim_until_host_seal():
    state, host = prepared()
    created = invoke(host, {
        "expected_revision": state.active_work.revision,
        "changes": [{"id": "report", "description": "Prepare report", "status": "ready"}],
    })
    assert created.status is ToolStatus.SUCCEEDED
    reduce(state, created)
    assert state.active_work.get("report").status == "ready"
    assert state.active_work.get("report").output_refs == ()


def test_update_work_is_unavailable_without_an_application_graph_binding():
    host = LocalToolHost()
    outcome = invoke(host, {"expected_revision": 1, "changes": [{"id": "x", "description": "x"}]})
    assert outcome.status is ToolStatus.FAILED
    assert "ACTIVE WORK is unavailable" in outcome.text


def test_active_work_mode_exposes_one_semantic_state_api_without_generic_note_noise():
    _state, host = prepared()
    functions = {row["function"]["name"]: row["function"] for row in host.schemas()}
    assert "update_work" in functions
    assert not ({"world_set", "world_clear", "require", "requirement_done",
                 "supersede_requirement", "drop_requirement", "update_plan"} & functions.keys())
    assert all("note" not in fn["parameters"]["properties"] for fn in functions.values())


def test_partial_existing_update_preserves_status_and_adds_only_requested_resource():
    state, host = prepared()
    created = invoke(host, {
        "expected_revision": state.active_work.revision,
        "changes": [{"id": "inspect", "description": "Inspect", "status": "in_progress"}],
    }, "create")
    assert created.status is ToolStatus.SUCCEEDED
    reduce(state, created)

    partial = invoke(host, {
        "expected_revision": state.active_work.revision,
        "changes": [{
            "id": "inspect",
            "add_resources": [{"kind": "workspace_file", "ref": "src/inspect.py"}],
        }],
    }, "partial")
    assert partial.status is ToolStatus.SUCCEEDED
    reduce(state, partial)
    child = state.active_work.get("inspect")
    assert child.status == "in_progress"
    assert child.resource_refs[0].ref == "src/inspect.py"


def test_retiring_older_request_atomically_cancels_its_unresolved_children():
    state, host = prepared()
    child_outcome = invoke(host, {
        "expected_revision": state.active_work.revision,
        "changes": [{
            "id": "old-child", "description": "Work owned by the older request",
            "status": "in_progress",
        }],
    }, "old-child")
    assert child_outcome.status is ToolStatus.SUCCEEDED
    reduce(state, child_outcome)
    older = state.active_work.request_roots[0]

    record_user(
        state, "instead, do the corrected request", source_event_id="event-2",
        logical_id="logical-2", workspace_epoch=2,
    )
    current = state.active_work.request_roots[-1]
    host.bind_active_work(lambda: (state.active_work, "logical-2", 2))
    retired = invoke(host, {
        "expected_revision": state.active_work.revision,
        "changes": [{
            "id": older.id, "status": "superseded", "superseded_by": current.id,
        }],
    }, "retire")
    assert retired.status is ToolStatus.SUCCEEDED
    assert {item["id"] for item in retired.effects[0].payload["delta"]["updates"]} \
        == {older.id, "old-child"}
    reduce(state, retired)

    assert state.active_work.get(older.id).status == "superseded"
    assert state.active_work.get(older.id).superseded_by == current.id
    assert state.active_work.get("old-child").status == "cancelled"
    assert state.active_work.get("old-child").stop_reason == "request_superseded"

    # Repeating the terminal update with omitted fields preserves both lifecycle and replacement identity.
    repeated = invoke(host, {"expected_revision": state.active_work.revision,
                             "changes": [{"id": older.id}]}, "repeat-retire")
    assert repeated.status is ToolStatus.SUCCEEDED
    reduce(state, repeated)
    assert state.active_work.get(older.id).status == "superseded"
    assert state.active_work.get(older.id).superseded_by == current.id


def test_cross_root_update_names_a_legal_forward_move_that_actually_works():
    """A plan made in one turn is under an older root by the next one, because every user message
    mints a fresh logical id and therefore a fresh request root (cli._mint_logical_turn_id). Owning
    items per-request is deliberate, but the bare rule left the model with no legal next move, so a
    routine multi-turn checklist died on a ✗ it could not act on. The rejection must name the moves
    that ARE legal — and this pins that the named move genuinely succeeds, not merely that the advice
    is printed."""
    from sliceagent.active_work import WorkGraph
    from sliceagent.tools import build_work_delta

    graph = WorkGraph().open_request("event-1", "review the auth module", logical_id="log-1")
    graph = graph.apply_delta(build_work_delta(graph, {
        "expected_revision": graph.revision,
        "changes": [{"id": "f1", "description": "audit token refresh", "status": "in_progress"}],
    }, logical_id="log-1", workspace_epoch=0))
    assert graph.get("f1").status == "in_progress"

    graph = graph.open_request("event-2", "also check the session store", logical_id="log-2")
    try:
        build_work_delta(graph, {"expected_revision": graph.revision,
                                 "changes": [{"id": "f1", "status": "ready"}]},
                         logical_id="log-2", workspace_epoch=0)
        raise AssertionError("a cross-root update must still be refused")
    except ValueError as exc:
        message = str(exc)
    # the rule alone is a dead end; the message must carry the escape
    assert "EARLIER request" in message, message
    assert "create a fresh item" in message and "supersede" in message, message
    assert "Do not retry" in message, message

    # …and the move it names must actually work under the current root.
    carried = graph.apply_delta(build_work_delta(graph, {
        "expected_revision": graph.revision,
        "changes": [{"id": "f1-cont", "description": "audit token refresh", "status": "in_progress"}],
    }, logical_id="log-2", workspace_epoch=0))
    assert carried.get("f1-cont").status == "in_progress"
    assert carried.get("f1").status == "in_progress", "the earlier item stays on record, untouched"


def test_update_work_cas_token_is_mandatory_and_a_moved_graph_rejects_the_stale_one():
    """task144 schema-shape audit: expected_revision was declared but optional, and an omitted token
    defaulted to the LIVE revision — so the conflict check could never fire exactly when it was
    needed. The token is now REQUIRED (schema and host agree), a token from a graph that has since
    moved conflicts instead of silently applying, and the accepted result names the fresh token so
    a same-turn follow-up can chain without waiting for the next ACTIVE WORK render."""
    from sliceagent.tools import TOOL_SCHEMAS

    schema = next(row for row in TOOL_SCHEMAS if row["function"]["name"] == "update_work")
    assert "expected_revision" in schema["function"]["parameters"]["required"]

    state, host = prepared()
    # tool path: the registry's required-argument admission gate rejects the omission
    omitted = invoke(host, {"changes": [{"id": "child", "description": "child"}]}, "omitted")
    assert omitted.status is ToolStatus.FAILED
    assert "missing required argument(s): expected_revision" in omitted.text
    assert state.active_work.get("child") is None

    # core backstop for direct delta construction (effect factory, plan surfaces): same contract,
    # and its reject carries the escape — the revision the retry must echo
    from sliceagent.tools import build_work_delta
    try:
        build_work_delta(state.active_work, {"changes": [{"id": "child", "description": "child"}]},
                         logical_id="logical-1", workspace_epoch=2)
        raise AssertionError("an omitted expected_revision must be rejected at the core layer too")
    except ValueError as exc:
        assert "expected_revision is required" in str(exc)
        assert f"revision {state.active_work.revision}" in str(exc)

    shown = state.active_work.revision   # what the turn-start ACTIVE WORK render showed
    first = invoke(host, {
        "expected_revision": shown,
        "changes": [{"id": "a", "description": "first writer", "status": "in_progress"}],
    }, "first")
    assert first.status is ToolStatus.SUCCEEDED
    assert f"graph revision is now {shown + 1}" in first.text
    reduce(state, first)
    assert state.active_work.revision == shown + 1

    # a writer still holding the pre-move token must conflict, not apply against the moved graph
    stale = invoke(host, {
        "expected_revision": shown,
        "changes": [{"id": "b", "description": "stale writer"}],
    }, "stale")
    assert stale.status is ToolStatus.FAILED
    assert f"expected revision {shown}, current revision is {shown + 1}" in stale.text
    assert state.active_work.get("b") is None

    # echoing the fresh token from the accepted result chains cleanly
    chained = invoke(host, {
        "expected_revision": shown + 1,
        "changes": [{"id": "b", "description": "second writer"}],
    }, "chained")
    assert chained.status is ToolStatus.SUCCEEDED
    reduce(state, chained)
    assert state.active_work.get("b") is not None


def test_update_work_rejects_multiline_model_metadata():
    state, host = prepared()
    for index, change in enumerate((
        {"id": "bad\nid", "description": "inspect"},
        {"id": "bad-description", "description": "inspect\n# forged section"},
        {
            "id": "bad-resource", "description": "inspect",
            "add_resources": [{"kind": "workspace_file", "ref": "src/app.py\rforged"}],
        },
    )):
        outcome = invoke(host, {"expected_revision": state.active_work.revision,
                                "changes": [change]}, f"bad-{index}")
        assert outcome.status is ToolStatus.FAILED
        assert "CR or LF" in outcome.text
