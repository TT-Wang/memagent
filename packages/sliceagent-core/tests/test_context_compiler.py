from __future__ import annotations

from dataclasses import replace

from sliceagent_core.active_work import EvidenceRef, OutputRef, ResourceRef, WorkDelta, WorkGraph, WorkItem
from sliceagent_core.context import (
    ContextBlock,
    EpistemicRole,
    Fidelity,
    FreshnessClass,
    InstructionClass,
    RepresentationLoss,
)
from sliceagent_core.context_compiler import (
    compile_active_context,
    dependency_resource_paths,
    render_active_work,
)
from sliceagent_core.pfc import Slice, record_user


def block(name: str, content: str | None = None) -> ContextBlock:
    return ContextBlock(
        block_id=f"region:{name}:full", item_id=f"region:{name}",
        alternative_group=f"region:{name}", priority=50,
        instruction_class=InstructionClass.TASK_STATE,
        freshness=FreshnessClass.DERIVED, fidelity=Fidelity.FULL,
        representation_loss=RepresentationLoss.NONE,
        content=content or f"#{name}\n", order=20, slot=3,
        epistemic_role=EpistemicRole.CONTROL_STATE,
    )


def graph_with_current_dependency() -> tuple[WorkGraph, dict[str, str]]:
    graph = WorkGraph().open_request("event-prior", "first exact request", logical_id="prior")
    graph = graph.open_request("event-current", "current exact request", logical_id="current")
    _prior, current = graph.request_roots
    child = WorkItem(
        id="inspect-file", root_id=current.id, source_refs=current.source_refs,
        description="Inspect the implementation", status="in_progress",
        resource_refs=(ResourceRef("workspace_file", "src/app.py", workspace_epoch=1),),
        evidence_refs=(EvidenceRef("tool_receipt", "invocation:read-1"),),
    )
    graph = graph.apply(WorkDelta(expected_revision=2, creates=(child,)))
    return graph, {"event-prior": "first exact request", "event-current": "current exact request"}


def test_active_work_renders_prior_source_but_current_request_only_by_reference():
    graph, sources = graph_with_current_dependency()
    text = render_active_work(graph, sources, current_logical_id="current")
    assert "first exact request" in text
    assert "current exact request" not in text
    assert "CURRENT REQUEST below (shown once)" in text
    assert "HOST-OWNED CURRENT REQUEST ROOT" in text
    assert "never pass this ID to update_work" in text
    assert "model-maintained description: Inspect the implementation" in text
    assert "workspace_file:src/app.py@workspace-1" in text

    mounted = render_active_work(
        graph, sources, current_logical_id="current",
        source_locator_prefix="@sliceagent/evidence/events",
    )
    assert "@sliceagent/evidence/events/event-prior.md" in mounted
    assert "@sliceagent/evidence/events/event-current.md" in mounted


def test_dependency_paths_are_selected_from_the_active_closure_only():
    graph, _sources = graph_with_current_dependency()
    delivered = graph.open_request("event-done", "done", logical_id="done")
    done_root = delivered.request_roots[-1]
    delivered = delivered.transition(
        done_root.id, "delivered",
        output_refs=(OutputRef("response", "done-response"),),
    )
    # A terminal root's stale resource never enters the unresolved closure.
    stale = replace(
        delivered.get(done_root.id),
        resource_refs=(ResourceRef("workspace_file", "stale.py", workspace_epoch=0),),
    )
    delivered = delivered.upsert(stale)
    assert dependency_resource_paths(delivered) == ("src/app.py",)
    assert dependency_resource_paths(delivered, workspace_epoch=0) == ()
    assert dependency_resource_paths(delivered, workspace_epoch=1) == ("src/app.py",)


def test_missing_prior_event_keeps_legacy_exact_intent_as_recovery_fallback():
    graph, sources = graph_with_current_dependency()
    sources.pop("event-prior")
    s = Slice(active_work=graph)
    compiled = compile_active_context(
        s,
        [block("intent"), block("task_objective"), block("world")],
        source_texts=sources,
        current_logical_id="current",
    )
    kept = {item.item_id for item in compiled}
    assert {"region:intent", "region:task_objective"} <= kept
    assert "region:world" not in kept


def test_record_user_opens_graph_only_at_explicit_application_ledger_seam():
    legacy = Slice(); legacy.reset("task")
    record_user(legacy, "hello", source_artifact="local-artifact")
    assert legacy.active_work == WorkGraph()

    active = Slice(); active.reset("task")
    record_user(
        active, "raw token=secret", source_artifact="local-artifact",
        source_event_id="event-1", source_text="raw token=[REDACT]",
        logical_id="logical-1", workspace_epoch=4,
    )
    root = active.active_work.request_roots[0]
    assert root.source_refs[0].extract("raw token=[REDACT]") == "raw token=[REDACT]"
    assert active.intent.current_request == "raw token=secret"
    assert root.workspace_epoch == 4
