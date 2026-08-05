from __future__ import annotations

from sliceagent.memory import NullMemory
from sliceagent.pfc import Slice, record_user
from sliceagent.seed import make_build_slice
from sliceagent.tools import LocalToolHost


class Retriever:
    def __init__(self):
        self.queries = []

    def retrieve(self, query, k=6):
        self.queries.append((query, k))
        return []


class Memory(NullMemory):
    def __init__(self):
        self.recalls = []

    def recall(self, query, k=6, paths=None):
        self.recalls.append((query, k, paths))
        return []


class Ledger:
    def user_sources(self):
        return {"event": "inspect this project"}


def state_with_graph():
    state = Slice(); state.reset("inspect this project")
    record_user(
        state, "inspect this project", source_artifact="local",
        source_event_id="event", logical_id="logical", workspace_epoch=0,
    )
    return state


def test_active_work_without_dependencies_does_not_eagerly_fetch_global_context(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\n", encoding="utf-8")
    state = state_with_graph()
    retriever, memory = Retriever(), Memory()
    seed = make_build_slice(
        state, LocalToolHost(str(tmp_path)), retriever, memory, state.goal,
        event_ledger=Ledger(), model_id="test-model",
    )()
    system, user = seed[0]["content"], seed[1]["content"]
    assert retriever.queries == [] and memory.recalls == []
    assert "# REPO MAP" not in system
    assert "# RELATED CODE" not in user and "# RELEVANT MEMORY" not in user
    assert user.count("inspect this project") == 1


