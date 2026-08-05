"""Spine SUBSTRATE gates — the seal machinery the tape consumes.

The spine REGION and its layout mode retired at tape graduation (docs/TAPE-GRADUATION.md wave 1;
historical arm: git tag lab-2026-08-05). What lives on — and is pinned here — is the substrate:
render_turn_digest (the ONE digest renderer feeding seal, recovery, and the tape), the R4
locator/key parity, load_session_spine (artifact-truth scan), R2 redaction through the seal, the
seal-vs-recovery byte parity, and the within-turn projection pin.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "sliceagent-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "sliceagent-cli", "src"))

from sliceagent_core.spine import _session_key, load_session_spine, render_turn_digest


def _digest(**over):
    base = dict(artifact_id="t-abc", session_id="sess-1", task_id="t1",
                status="completed", user_request="fix the divide bug")
    base.update(over)
    return render_turn_digest(**base)


def test_renderer_deterministic_and_timestamp_free():
    a = _digest(files=["b.py", "a.py"], title="Guarded  zero\ndivision")
    b = _digest(files=["a.py", "b.py"], title="Guarded  zero\ndivision")
    assert a == b                       # order-insensitive inputs, byte-identical output
    assert "files: a.py, b.py" in a
    assert "note: Guarded zero division" in a


def test_ask_cap_is_loud_never_silent():
    big = "x" * 5000
    d = _digest(user_request=big)
    assert big[:100] in d
    assert "…[+3000 chars in sealed turn]" in d


def test_continuation_segment_dedupes_by_reference():
    d = _digest(segment_index=1, segment_outcome="workspace_transition", logical_turn_id="L7")
    assert "(continuation of logical turn L7)" in d
    assert "fix the divide bug" not in d          # the repeated ask is never re-embedded
    assert "seg 1:workspace_transition" in d


def test_locator_cites_artifact_id_and_key_parity():
    d = _digest(session_id="weird session id!!")
    key = _session_key("weird session id!!")
    assert f'read_file("@sliceagent/history/sessions/{key}/t-abc.md")' in d
    from sliceagent_core.contextfs import ArtifactHistoryProvider
    for sid in ("plain-id", "weird session id!!", "", "A" * 200):
        assert _session_key(sid) == ArtifactHistoryProvider._session_key(sid), sid


class _Art:
    def __init__(self, id, kind, session_id, digest, order_ns):
        self.id, self.kind, self.session_id = id, kind, session_id
        self.structured_body = {"spine_digest": digest, "meta": {"order_ns": order_ns}}
        self.timestamp = ""


def test_load_scan_filters_and_orders():
    arts = [
        _Art("t-2", "turn", "s", "SECOND\n", 200),
        _Art("t-x", "turn", "OTHER", "WRONG SESSION\n", 50),
        _Art("subagent-1", "subagent", "s", "CHILD\n", 60),
        _Art("t-1", "turn", "s", "FIRST\n", 100),
        _Art("t-old", "turn", "s", None, 10),          # pre-spine artifact: skipped, not crashed
    ]
    assert load_session_spine(arts, "s") == ["FIRST\n", "SECOND\n"]


def _store(root, session="session-1"):
    from sliceagent_core.runtime_persistence import LocalTurnStore
    import pathlib
    root = pathlib.Path(root); root.mkdir(parents=True, exist_ok=True)
    workspace = str(root / "ws"); (root / "ws").mkdir(exist_ok=True)
    return LocalTurnStore(workspace, session, store_root=str(root / "store"))


def test_seeded_secret_absent_from_spine_bytes_and_artifact(tmp_path):
    """R2: seeded secret in the ask -> absent from digest bytes and artifact alike."""
    secret = "sk-test-secret-0123456789abcdef"
    store = _store(tmp_path)
    active = store.begin(task_id="task-A", logical_id="turn-1",
                         user_request=f"deploy with token {secret} now")
    store.seal(state={}, record={}, status="end_turn", title=f"used {secret}",
               files=("a.py",))
    artifact = store.coordinator.artifacts.get(active.artifact_id)
    digest = artifact.structured_body["spine_digest"]
    assert secret not in digest
    assert secret not in str(artifact.to_dict())
    assert "deploy with token" in digest          # the ask survives, only the secret is masked


def test_seal_and_recovery_share_renderer_byte_parity(tmp_path):
    """Seal-path render == journal-only recovery render through the ONE renderer (R3)."""
    store = _store(tmp_path / "a")
    active = store.begin(task_id="task-A", logical_id="turn-1", user_request="fix the bug")
    store.seal(state={}, record={}, status="end_turn", title="fixed", files=("b.py",))
    artifact = store.coordinator.artifacts.get(active.artifact_id)
    expected = render_turn_digest(
        artifact_id=active.artifact_id, session_id="session-1", task_id="task-A",
        status=artifact.status, user_request="fix the bug",
        logical_turn_id="turn-1", segment_index=0, title="fixed", files=("b.py",),
    )
    assert artifact.structured_body["spine_digest"] == expected
    crashed = _store(tmp_path / "b")
    crash = crashed.begin(task_id="task-A", logical_id="turn-1", user_request="continue the fix")
    crashed.close()
    recovered = _store(tmp_path / "b", session="session-2")
    result = recovered.recover_pending()[0]
    rec_artifact = recovered.coordinator.artifacts.get(result.artifact_id)
    assert result.artifact_id == crash.artifact_id
    assert rec_artifact.structured_body["spine_digest"] == render_turn_digest(
        artifact_id=crash.artifact_id, session_id="session-1", task_id="task-A",
        status="interrupted", user_request="continue the fix",
    )
    spine = load_session_spine([artifact, rec_artifact], "session-1")
    assert spine == [artifact.structured_body["spine_digest"],
                     rec_artifact.structured_body["spine_digest"]]


def _mini_slice(spine=("[turn t-1 · task t · completed]\nask: earlier thing\n",)):
    from sliceagent_core.pfc import Slice, record_user
    st = Slice()
    st.reset("current ask")
    record_user(st, "current ask")
    st.continuity.session_spine = list(spine)
    return st


def test_projection_pin_keeps_msg1_byte_stable_within_turn(monkeypatch):
    """P5 within-turn finding: unconditional per-call re-projection mutated msg1's tail as capacity
    shrank, re-billing the whole trajectory each step. The pin reuses the turn's projection
    verbatim while it fits, and re-projects only on real overflow."""
    import json as _json
    from sliceagent_core.context import SeedPlan
    from sliceagent_core.loop import _project_request_seed
    from sliceagent_core.regions import build_context_blocks, render_context_selection
    from sliceagent_core.seed import _slice_context
    monkeypatch.delenv("AGENT_CONTEXT_WINDOW", raising=False)
    st = _mini_slice()
    blocks = build_context_blocks(_slice_context(st, artifacts="x" * 4000, open_file_paths=()))
    plan = SeedPlan(system="sys", blocks=blocks, render_blocks=render_context_selection,
                    request_block="\n\nREQ", now_block="\nNOW")

    class _Llm:
        context_window = 200_000
        max_tokens = 100
    llm = _Llm()
    first = _project_request_seed(plan, [], llm, [])
    grown = [{"role": "assistant", "content": "step"}, {"role": "tool", "content": "out " * 100}]
    second = _project_request_seed(plan, grown, llm, [])
    assert _json.dumps(first) == _json.dumps(second)      # byte-identical msg1, whole turn
    llm.context_window = 2000
    third = _project_request_seed(plan, grown, llm, [])
    assert _json.dumps(third) != _json.dumps(first)
    assert len(_json.dumps(third)) < len(_json.dumps(first))
    monkeypatch.setenv("AGENT_PIN_PROJECTION", "0")
    llm.context_window = 200_000
    unpinned = _project_request_seed(plan, [], llm, [])
    assert isinstance(unpinned, list) and unpinned[0]["role"] == "system"
