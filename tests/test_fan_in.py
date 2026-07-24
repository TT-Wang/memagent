from __future__ import annotations

"""Live fan-in helper slivers plus proof there is no fan-in context path.

New child computation returns directly in its tool result, so neither the seed
nor the elastic region compiler mounts a synthetic fan-in bundle.
"""

from sliceagent.active_work import WorkDelta, WorkItem
from sliceagent.context import ResourceKind, ResourceRef
from sliceagent.fan_in import (
    artifact_read_coverage,
    artifact_view_kind,
    canonical_artifact_id,
    normalize_evidence_account,
    normalize_evidence_status,
)
from sliceagent.memory import NullMemory
from sliceagent.pfc import Slice, record_user
from sliceagent.regions import build_context_blocks
from sliceagent.seed import _slice_context, make_build_slice
from sliceagent.tools import LocalToolHost


def _state_with_child():
    state = Slice()
    state.reset("delegate one review")
    record_user(state, "delegate one review", source_event_id="event", logical_id="logical")
    root = state.active_work.request_roots[-1]
    child = WorkItem(
        id="review-child",
        root_id=root.id,
        source_refs=root.source_refs,
        description="Review the parser",
        status="ready",
    )
    state.active_work = state.active_work.apply(
        WorkDelta(expected_revision=state.active_work.revision, creates=(child,))
    )
    return state


def _bundle_call(index: int, disposition: str) -> dict:
    if disposition == "complete":
        return {
            "id": f"spawn-{index}",
            "status": "succeeded",
            "child_artifact_id": f"sub-{index}",
            "child_work_item_id": f"review-{index}",
            "child_operational_status": "succeeded",
            "child_evidence_declared": True,
            "child_evidence_status": "content_retained",
            "child_source_coverage_status": "source_complete",
        }
    if disposition == "partial":
        return {
            "id": f"spawn-{index}",
            "status": "succeeded",
            "child_artifact_id": f"sub-{index}",
            "child_work_item_id": f"review-{index}",
            "child_operational_status": "succeeded",
            "child_evidence_declared": True,
            "child_evidence_status": "content_partial",
            "child_source_coverage_status": "source_partial",
        }
    return {
        "id": f"spawn-{index}",
        "status": "failed",
        "child_artifact_id": f"sub-{index}",
        "child_work_item_id": f"review-{index}",
        "child_operational_status": "failed",
        "child_evidence_declared": True,
        "child_evidence_status": "content_partial",
    }


def test_artifact_helpers_preserve_exact_identity_and_view_kind():
    handle = "artifacts/sub-1/evidence/obs-001-page-001.md"
    assert canonical_artifact_id("artifact", handle) == "sub-1"
    assert artifact_view_kind("artifact", handle) == "evidence"
    assert artifact_view_kind("artifact", "artifacts/sub-1.md") == "report"
    report_with_marker_prose = "Finding: code emits [truncated; bytes omitted] and says paged out."
    assert artifact_read_coverage(
        {},
        report_with_marker_prose,
        resource_kind="artifact",
        handle="artifacts/sub-1.md",
    ) == "complete"


def test_evidence_account_is_bounded_and_tolerant():
    assert normalize_evidence_status("none") == "none"
    assert normalize_evidence_status("navigation_only") == "navigation_only"
    assert normalize_evidence_status("future-new-value") == "not_assessed"
    account = normalize_evidence_account({
        "status": "content_partial",
        "content_success_count": 999999,
        "truncated_content_view_count": 2,
        "scope_paths": [f"src/{index}.py" for index in range(100)],
        "content_paths": "malformed",
    })
    assert account["status"] == "content_partial"
    assert account["content_success_count"] == 10_000
    assert len(account["scope_paths"]) == 16
    assert "content_paths" not in account
    assert normalize_evidence_account("not a mapping") == {}


def test_read_effect_keeps_artifact_coverage_proofs(tmp_path):
    host = LocalToolHost(str(tmp_path))
    host.resource_ref = lambda _path: ResourceRef(ResourceKind.ARTIFACT, "artifacts/sub-1.md")
    try:
        from sliceagent.execution import ToolInvocation, ToolStatus

        full = host._read_resource_effects(
            ToolInvocation("read-full", "read_file", {"path": "artifacts/sub-1.md"}, 0),
            ToolStatus.SUCCEEDED,
            "whole report",
        )[0].payload
        partial = host._read_resource_effects(
            ToolInvocation(
                "read-page",
                "read_file",
                {"path": "artifacts/sub-1.md", "limit": 10},
                0,
            ),
            ToolStatus.SUCCEEDED,
            "first page",
        )[0].payload
    finally:
        host.cleanup()
    assert full["artifact_id"] == "sub-1"
    assert full["read_coverage"] == "complete"
    assert len(full["content_sha256"]) == 64
    assert full["content_bytes"] == len("whole report")
    assert partial["read_coverage"] == "partial"


def test_no_fan_in_region_is_registered_as_a_live_context_region():
    state = _state_with_child()
    state.runtime.recent_calls = [
        _bundle_call(1, "complete") | {"child_work_item_id": "review-child"}
    ]

    blocks = build_context_blocks(_slice_context(state, "(no files opened yet)"))
    ids = {block.item_id for block in blocks}
    assert "region:fan_in" not in ids
    rendered = "\n".join(block.content for block in blocks)
    assert "# DELEGATION FAN-IN" not in rendered
    assert "HOST FAN-IN" not in rendered


def test_production_seed_does_not_reload_or_inject_old_child_reports(tmp_path):
    from sliceagent.contextfs import ArtifactContextProvider
    from sliceagent.persistence import Artifact, ArtifactStore
    from sliceagent.runtime_persistence import CoreArtifactFS

    state = _state_with_child()
    state.runtime.recent_calls = [
        _bundle_call(1, "complete") | {"child_work_item_id": "review-child"}
    ]
    report = "FULL CANONICAL CHILD REPORT\nP2: parser rejects a valid empty input."
    store = ArtifactStore(str(tmp_path / "artifacts"))
    store.put(Artifact(
        id="sub-1",
        kind="subagent",
        workspace_id="workspace",
        session_id="session",
        task_id="task",
        status="ok",
        summary=report,
        structured_body={"report": report, "observations": [], "claims": []},
    ))
    core = CoreArtifactFS(store)
    host = LocalToolHost(str(tmp_path))
    host._artifacts = core
    host._contextfs.mount(
        "evidence/children",
        ArtifactContextProvider(
            core,
            kinds=("subagent",),
            canonical_mount="@sliceagent/evidence/children",
            title="CHILDREN",
        ),
    )
    try:
        seed = make_build_slice(
            state,
            host,
            None,
            NullMemory(),
            state.goal,
            session_id="session",
        )()
    finally:
        host.cleanup()

    rendered = "\n".join(str(message.get("content") or "") for message in seed)
    assert report not in rendered
    assert "# DELEGATION FAN-IN" not in rendered
    assert "HOST FAN-IN" not in rendered
