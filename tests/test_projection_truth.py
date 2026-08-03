"""Projection truth: virtual resources, live capability guidance, and prompt A/B seam."""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent.events import ToolResult  # noqa: E402
from sliceagent.code_grep import make_grep_tool  # noqa: E402
from sliceagent.agents import BUILTIN_AGENTS  # noqa: E402
from sliceagent.execution import ToolInvocation  # noqa: E402
from sliceagent.intent import analyze_turn  # noqa: E402
from sliceagent.pfc import Slice, slice_sink  # noqa: E402
from sliceagent.prompt import (MEMORY_ACCUMULATE, memory_model_for_eval,
                               render_delegation_guidance)  # noqa: E402
from sliceagent.regions import build_context_blocks  # noqa: E402
from sliceagent.runtime_persistence import CoreArtifactFS  # noqa: E402
from sliceagent.seed import (_slice_context, build_artifacts,
                             physical_active_files)  # noqa: E402
from sliceagent.scoped_agent import ScopedSurface, allowed_for  # noqa: E402
from sliceagent.scoped_spawn import ScopedSpawnHost             # noqa: E402
from sliceagent.tools import LocalToolHost  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn




class _ArtifactView:
    def read_file(self, path):
        return f"sealed virtual artifact: {path}"


class _ArtifactStore:
    def __init__(self):
        self.artifact = SimpleNamespace(
            id="turn-absolute", kind="turn", title="absolute route", task_id="task-1",
            status="completed", timestamp="2026-07-11T00:00:00Z",
            brief={"request": "find the canonical needle"}, summary="canonical virtual needle",
            structured_body={"assistant": "canonical virtual needle"}, refs=(),
        )

    def list_all(self):
        return [self.artifact]

    def get(self, artifact_id):
        if artifact_id != self.artifact.id:
            raise KeyError(artifact_id)
        return self.artifact


def _invoke_read(host, path, invocation_id="read-1"):
    invocation = ToolInvocation(invocation_id, "read_file", {"path": path}, 0)
    outcome = host.registry.invoke(invocation)
    return outcome


@check
def virtual_artifact_read_stays_typed_and_never_enters_open_files():
    host = LocalToolHost(tempfile.mkdtemp())
    host._artifacts = _ArtifactView()
    outcome = _invoke_read(host, "artifacts/turn-1.md")
    resource = next(effect for effect in outcome.effects if effect.kind == "resource_observed")
    assert resource.payload["resource_kind"] == "artifact"
    assert resource.payload["handle"] == "artifacts/turn-1.md"
    assert resource.payload["artifact_id"] == "turn-1"
    assert resource.payload["read_coverage"] == "complete"
    assert len(resource.payload["content_sha256"]) == 64
    assert resource.payload["content_bytes"] == len(outcome.text.encode("utf-8"))

    state = Slice(); state.reset("recall")
    slice_sink(state)(ToolResult(
        "read_file", {"path": "artifacts/turn-1.md"}, outcome.text, outcome.failing,
        status=outcome.status.value, invocation_id=outcome.invocation.id, outcome=outcome,
    ))
    assert state.active_files == []

    # A poisoned legacy checkpoint is filtered defensively too.
    state.active_files = ["artifacts/turn-1.md"]
    rendered = build_artifacts(state, host)
    assert rendered == "(no workspace files opened yet)" and "not created yet" not in rendered


@check
def child_evidence_page_read_keeps_root_artifact_provenance_but_is_not_a_report_read():
    from sliceagent.persistence import Artifact, ArtifactStore

    store = ArtifactStore(tempfile.mkdtemp(prefix="projection-child-evidence-"))
    view = "     1\treturn verified_value"
    store.put(Artifact(
        id="subagent-evidence-root", kind="subagent", workspace_id="workspace",
        session_id="session", task_id="task", status="ok",
        structured_body={
            "report": "verified_value is returned",
            "observations": [{
                "v": 1, "tool": "read_file", "args": {"path": "value.py"},
                "status": "succeeded", "view": view,
                "redacted": False, "truncated": False,
            }],
        },
    ))
    host = LocalToolHost(tempfile.mkdtemp(prefix="projection-child-workspace-"))
    host._artifacts = CoreArtifactFS(store)
    path = "artifacts/subagent-evidence-root/evidence/obs-001-page-001.md"
    outcome = _invoke_read(host, path, "read-child-evidence")
    resource = next(effect for effect in outcome.effects if effect.kind == "resource_observed")
    assert resource.payload["artifact_id"] == "subagent-evidence-root"
    assert resource.payload["artifact_view"] == "evidence"
    assert resource.payload["read_coverage"] == "complete"

    state = Slice(); state.reset("verify child evidence")
    slice_sink(state)(ToolResult(
        "read_file", {"path": path}, outcome.text, outcome.failing,
        status=outcome.status.value, invocation_id=outcome.invocation.id, outcome=outcome,
    ))
    assert state.active_files == []
    assert state.runtime.recent_calls[-1]["observed_artifact_id"] == "subagent-evidence-root"
    assert state.runtime.recent_calls[-1]["observed_artifact_view"] == "evidence"




@check
def absolute_artifact_paths_share_the_canonical_virtual_handle_for_read_list_and_grep():
    root = tempfile.mkdtemp()
    host = LocalToolHost(root)
    host._artifacts = CoreArtifactFS(_ArtifactStore())
    host.registry.register(make_grep_tool(host))
    absolute_mount = os.path.join(root, "artifacts")
    absolute_file = os.path.join(absolute_mount, "turn-absolute.md")

    outcome = _invoke_read(host, absolute_file, "read-absolute")
    assert "canonical virtual needle" in outcome.text
    resource = next(effect for effect in outcome.effects if effect.kind == "resource_observed")
    assert resource.payload["resource_kind"] == "artifact"
    assert resource.payload["handle"] == "artifacts/turn-absolute.md"
    assert resource.payload["artifact_id"] == "turn-absolute"
    assert resource.payload["read_coverage"] == "complete"
    assert "turn-absolute.md" in host.run("list_files", {"path": absolute_mount})
    grep = host.run("grep", {"pattern": "canonical virtual needle", "path": absolute_file})
    assert "artifacts/turn-absolute.md:" in grep

    blocked = host.run("edit_file", {"path": absolute_file, "content": "shadow"})
    assert "read-only authoritative local artifact archive" in blocked
    assert not os.path.exists(absolute_file)


@check
def absolute_physical_artifact_file_still_shadows_the_virtual_handle():
    root = tempfile.mkdtemp()
    physical = os.path.join(root, "artifacts", "turn-absolute.md")
    os.makedirs(os.path.dirname(physical))
    with open(physical, "w", encoding="utf-8") as stream:
        stream.write("physical workspace bytes")
    host = LocalToolHost(root)
    host._artifacts = CoreArtifactFS(_ArtifactStore())

    outcome = _invoke_read(host, physical, "read-absolute-shadow")
    assert "physical workspace bytes" in outcome.text
    assert "canonical virtual needle" not in outcome.text
    resource = next(effect for effect in outcome.effects if effect.kind == "resource_observed")
    assert resource.payload == {"resource_kind": "workspace_file", "handle": physical}


@check
def child_isolation_and_observation_capsules_follow_canonical_host_routing():
    root = tempfile.mkdtemp()
    host = LocalToolHost(root)
    host._artifacts = CoreArtifactFS(_ArtifactStore())
    absolute_mount = os.path.join(root, "artifacts")
    absolute_file = os.path.join(absolute_mount, "turn-absolute.md")
    child = ScopedSurface(host, allowed_for(BUILTIN_AGENTS["explorer"], host))

    # Absolute and relative spellings route to the same virtual archive and are both parent-private.
    for path in ("artifacts/turn-absolute.md", absolute_file):
        blocked = child.run("read_file", {"path": path})
        assert "private namespace" in blocked, (path, blocked)
    blocked_list = child.run("list_files", {"path": absolute_mount})
    assert "private namespace" in blocked_list, blocked_list

    # Once real project bytes shadow the mount, canonical routing classifies them as physical. Child
    # reads must remain available; a lexical `artifacts/` deny would get this wrong.
    os.makedirs(absolute_mount, exist_ok=True)
    with open(absolute_file, "w", encoding="utf-8") as stream:
        stream.write("physical child-visible bytes")
    assert "physical child-visible bytes" in child.run("read_file", {"path": absolute_file})
    assert "turn-absolute.md" in child.run("list_files", {"path": absolute_mount})

    # The host-private physical store is not a project shadow and stays isolated under its absolute spelling.
    private_file = os.path.join(root, ".sliceagent", "blobs", "parent.txt")
    os.makedirs(os.path.dirname(private_file), exist_ok=True)
    with open(private_file, "w", encoding="utf-8") as stream:
        stream.write("parent-private")
    assert "private namespace" in child.run("read_file", {"path": private_file})


@check
def real_workspace_file_shadowing_reserved_mount_remains_physical():
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "artifacts"))
    with open(os.path.join(root, "artifacts", "real.md"), "w", encoding="utf-8") as stream:
        stream.write("real workspace bytes\n" + ("payload\n" * 80))
    host = LocalToolHost(root); host._artifacts = _ArtifactView()
    outcome = _invoke_read(host, "artifacts/real.md", "read-real")
    resource = next(effect for effect in outcome.effects if effect.kind == "resource_observed")
    assert resource.payload["resource_kind"] == "workspace_file"
    state = Slice(); state.reset("read")
    slice_sink(state)(ToolResult(
        "read_file", {"path": "artifacts/real.md"}, outcome.text, outcome.failing,
        status=outcome.status.value, invocation_id=outcome.invocation.id, outcome=outcome,
    ))
    assert state.active_files == ["artifacts/real.md"]
    assert "real workspace bytes" in build_artifacts(state, host)
    paths = physical_active_files(state, host)
    blocks = build_context_blocks(_slice_context(
        state, build_artifacts(state, host), open_file_paths=paths,
    ))
    locator = next(block for block in blocks
                   if block.item_id == "region:open_files" and block.fidelity.value == "locator")
    assert 'read_file("artifacts/real.md")' in locator.content
    assert locator.resource_refs[0].kind.value == "workspace_file"


class _Inner:
    def schemas(self):
        return []

    def accesses(self, _name, _args):
        return []

    def run(self, _name, _args):
        return ""


@check
def delegation_guidance_is_compiled_from_the_live_spawn_schema():
    host = ScopedSpawnHost(_Inner(), llm=None, retriever=None, memory=None)
    schema = next(s for s in host.schemas() if s["function"]["name"] == "spawn_agent")
    props = schema["function"]["parameters"]["properties"]
    assert set(props) == {"agent", "task", "work_item_id", "scope", "exclusions", "background"}
    kinds = props["agent"]["enum"]
    assert "explorer" in kinds and "general" in kinds
    assert "name" not in props and "grants" not in props
    text = render_delegation_guidance(host.schemas())
    assert "Available agent kinds: " in text and "explorer" in text and "general" in text
    assert "standing specialist" not in text and "grants field" not in text
    assert "complete normalized report directly as this tool result" in text
    assert "archive and evidence locators" in text and "not required for delivery" in text
    assert "ignore-aware source map" in text
    assert "20-30k source tokens" in text and "80-120 KB" in text
    assert "typed scope field" in text
    assert "scheduler owns those physical waves" in text
    assert "user explicitly requests a child count" in text
    assert "blindly reading every file in full" in text
    assert "coverage gaps" in text and "cite the sources" in text
    assert "work_item_id" not in text and "DELEGATION FAN-IN" not in text


@check
def memory_model_file_replaces_only_the_contract_and_allows_empty_arm():
    prior = os.environ.get("SLICEAGENT_MEMORY_MODEL_FILE")
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as stream:
            stream.write("# TEST OPERATING CONTRACT\nexact arm")
            path = stream.name
        os.environ["SLICEAGENT_MEMORY_MODEL_FILE"] = path
        assert memory_model_for_eval(MEMORY_ACCUMULATE) == "# TEST OPERATING CONTRACT\nexact arm"
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("")
        assert memory_model_for_eval(MEMORY_ACCUMULATE) == ""
    finally:
        if prior is None:
            os.environ.pop("SLICEAGENT_MEMORY_MODEL_FILE", None)
        else:
            os.environ["SLICEAGENT_MEMORY_MODEL_FILE"] = prior
        try:
            os.unlink(path)
        except (NameError, OSError):
            pass

    assert "directly obeying requested delegation/scope" in MEMORY_ACCUMULATE
    assert "No supported response-quality issue is evidenced" in MEMORY_ACCUMULATE












@check
def source_needs_gating_survives_on_the_live_admission_producer():
    # The interpret_turn-driven evidence-pipeline tests died with the discourse cone; the SOURCE-NEEDS
    # region gating they exercised is LIVE (regions._region_selected_by_source_needs) and is driven by
    # analyze_turn (intent.py) — the still-alive producer. An utterance-recall request must select the
    # sealed-past furniture (contract + manifest) and exclude the roomy current-world regions.
    recall = analyze_turn("What did you say in your previous response?")
    assert recall.source_needs == ("prior_assistant_utterance",), recall.source_needs
    state = Slice(); state.reset("Review the project")
    state.intent.current_request = "What did you say in your previous response?"
    state.intent.turn_admission = recall
    names = {block.item_id for block in build_context_blocks(_slice_context(
        state, "# unrelated.py\n1: live bytes", discovery="unrelated code",
        memory="unrelated memory", worktree="branch main",
        cache_manifest='turn 1 → read_file("history/turn-1.md")',
    ))}
    assert "region:cache_manifest" in names and "region:turn_contract" in names, names
    assert not {
        "region:open_files", "region:related_code", "region:memory",
        "region:world", "region:worktree",
    }.intersection(names), names


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception as error:  # noqa: BLE001
            failed += 1; print(f"FAIL {fn.__name__}: {type(error).__name__}: {error}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
