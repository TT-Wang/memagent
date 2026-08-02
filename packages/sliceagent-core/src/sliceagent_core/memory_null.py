"""NullMemory — the no-op Memory default (no vault). A TRUE no-op: every method inert
(no I/O, no clock), so the eval path is deterministic and adds nothing to the slice.
Lives in core as the Memory contract's deterministic default/test seam; real memory
implementations (the Memem stack) are blocks the host injects (sliceagent_cli.memory).
"""
from .interfaces import Snippet, TaskRef, TaskState  # noqa: F401 (contract types)


class NullMemory:
    """No durable memory (the default until a vault is configured). A TRUE no-op — every method is
    inert (no I/O, no clock), so the eval path is deterministic and adds nothing to the slice."""

    is_durable = False

    def recall(self, query: str, k: int = 6, paths: list[str] | None = None) -> list[Snippet]:
        return []

    def remember(self, content: str, *, title: str = "", scope: str = "default", tags: str = "",
                 paths: list[str] | None = None) -> None:
        return None

    def append_episode(self, session_id: str, task_id: str, turn: int, record: dict) -> None:
        return None

    def read_episodes(self, session_id: str, *, limit: int | None = None) -> list[dict]:
        return []

    def episode_manifest(self, session_id: str, k: int) -> tuple[list[dict], int]:
        return [], 0

    def append_subagent_artifact(self, session_id: str, artifact: dict) -> str:
        return ""   # no vault → not archived; the spawn host falls back to the inline report

    def read_subagent_artifacts(self, session_id: str) -> list[dict]:
        return []

    def index_subagent_artifact(self, session_id: str, handle: str, artifact: dict) -> None:
        return None   # no FTS index without a vault

    def search_episodes(self, query: str, *, limit: int = 5,
                        exclude_session: str | None = None,
                        only_session: str | None = None) -> list[dict]:
        return []

    def checkpoint_task(self, task: TaskState) -> None:
        return None

    def load_task(self, task_id: str) -> TaskState | None:
        return None

    def list_session_tasks(self, session_id: str) -> list[TaskRef]:
        return []

    def mark_used(self, memory_id: str) -> None:
        return None

    def consolidate(self, session_id: str, *, llm=None, mode: str = "deterministic") -> dict:
        return {"lessons": 0, "skills": 0, "skills_rejected": 0, "errors": 0}

    def close(self) -> None:
        return None
