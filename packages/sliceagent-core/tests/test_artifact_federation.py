from __future__ import annotations

import tempfile

from sliceagent_core.persistence import Artifact, ArtifactStore
from sliceagent_core.runtime_persistence import CoreArtifactFS


def test_exact_artifact_handle_faults_across_workspaces_without_listing_the_archive():
    archive = tempfile.mkdtemp(prefix="artifact-federation-")
    source = ArtifactStore(f"{archive}/workspace-a")
    target = ArtifactStore(f"{archive}/workspace-b")
    source.put(Artifact(
        id="turn-source-receipt", kind="turn", workspace_id="workspace-a",
        session_id="session", task_id="task", title="Source receipt",
        brief={"request": "switch and inspect"},
        structured_body={"assistant": "source result", "markdown": "exact source evidence"},
    ))

    virtual = CoreArtifactFS(target, archive_root=archive)
    rendered = virtual.read_file("artifacts/turn-source-receipt.md")
    assert "exact source evidence" in rendered
    assert "turn-source-receipt.md" not in virtual.index()
    assert "turn-source-receipt.md" not in virtual.listing()


def test_federation_is_disabled_without_an_explicit_archive_root():
    archive = tempfile.mkdtemp(prefix="artifact-local-only-")
    source = ArtifactStore(f"{archive}/workspace-a")
    target = ArtifactStore(f"{archive}/workspace-b")
    source.put(Artifact(
        id="child-other-workspace", kind="subagent", workspace_id="workspace-a",
        session_id="session", task_id="task", structured_body={"markdown": "child proof"},
    ))
    assert "no such retained artifact" in CoreArtifactFS(target).read_file(
        "artifacts/child-other-workspace.md",
    )


def test_core_artifact_renderer_never_doubles_an_already_rendered_reference_handle():
    artifact = Artifact(
        id="subagent-synthesis", kind="subagent", workspace_id="workspace",
        session_id="session", task_id="task", refs=("artifacts/subagent-source.md",),
        structured_body={"report": "synthesis"},
    )
    rendered = CoreArtifactFS._render(artifact)
    assert 'read_file("artifacts/subagent-source.md")' in rendered
    assert "artifacts/artifacts/" not in rendered and ".md.md" not in rendered
