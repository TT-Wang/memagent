"""The contracts the core depends on — never the implementations.

The moat (loop + tiers) talks only to these. Everything commodity (LLM I/O,
retrieval, tool execution/sandbox, verification) lives behind them and is swappable.
Verification, budgets, and the catastrophic-command floor are supplied via hooks.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import unicodedata as _unicodedata
from typing import Protocol, runtime_checkable


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class AssistantMessage:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict | None = None            # {"prompt_tokens": int, "completion_tokens": int}
    finish_reason: str | None = None     # provider's raw finish reason → normalized by the loop
    # Provider-private reasoning bytes required to continue some tool-call protocols (not presentation text).
    # DeepSeek V4 thinking mode returns this alongside content and rejects the next tool-result request if the
    # preceding assistant message omits it. Kept last so every existing positional construction stays valid.
    reasoning_content: str | None = None


@dataclass
class Snippet:
    path: str
    text: str
    score: float = 0.0


@dataclass
class PageRef:
    """A bounded reference to one PAGE the PageTable can surface into the slice — the unified shape
    every read/retrieval backend (code map, project-notes, cross-session episodes) returns from
    PageTable.lookup(). Carries RAW text (`preview`); the renderer fences it (wrap_untrusted) so
    injection-fencing stays at ONE layer. `handle` locates the page (a repo-map marker, a subtree
    path, a session·turn locator); `untrusted` flags re-injected external content (default True)."""
    handle: str
    kind: str
    preview: str
    score: float = 0.0
    untrusted: bool = True


@dataclass
class TaskRef:
    """A bounded index row for the OTHER OPEN THREADS tier (Step 3)."""
    task_id: str
    title: str
    status: str            # active | parked | done | abandoned
    updated: str = ""


@dataclass
class TaskState:
    """Resumable, distilled state for one task = the serializable Slice fields. Stores REFS
    (file paths + anchors), never file contents — ground truth is re-read from disk on resume.
    Transient tiers (recent, action_log, active_skills) are intentionally NOT serialized."""
    task_id: str
    schema_version: int = 2
    session_id: str = ""
    title: str = ""
    status: str = "active"
    goal: str = ""
    goal_source: str = ""
    objective_status: str = "active"
    findings: list[str] = field(default_factory=list)
    finding_source: dict[str, str] = field(default_factory=dict)  # finding -> provenance tier (carried; else resume upgrades 'claim'→'tool-note')
    # v2 typed intent. `requirements` remains a derived v1 compatibility field for old checkpoint readers;
    # when intent_entries is present it is authoritative and taskstate ignores requirements on restore.
    current_request: str = ""
    # Workspace frame that authored the latest local checkpoint. Restoring it prevents old-epoch resources
    # from becoming current merely because a fresh Session counter starts at zero.
    workspace_epoch: int = 0
    # Source-linked Active Work graph records.  This is the durable semantic frontier, not a
    # transcript; exact user bytes remain owned by the application event ledger/artifact store.
    # Absence is the backwards-compatible representation for pre-Active-Work checkpoints.
    active_work: list[dict] = field(default_factory=list)
    intent_entries: list[dict] = field(default_factory=list)
    intent_next_id: int = 1
    requirements: list[dict] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)          # PLAN / TodoWrite steps + status (carried)
    progress_signals: list[dict] = field(default_factory=list)  # small task-scoped semantic ring
    deliverable_requirement: dict | None = None                 # typed L1 output envelope for one logical request
    open_report: str = ""                                   # OPEN USER REPORT blocker (carried; the "it's broken" push-back must survive resume)
    reconciliation_required: str = ""                       # advisory evidence: an earlier effect is still unknown
    reconciliation_targets: list[str] = field(default_factory=list)
    world: dict = field(default_factory=dict)               # agent WORLD MODEL (carried; was dropped on resume)
    active_files: list[str] = field(default_factory=list)
    edited_files: list[str] = field(default_factory=list)   # list on the wire; a set in the Slice
    edit_anchor: dict[str, str] = field(default_factory=dict)
    last_error: str = ""
    since_edit: int = 0
    links: list[str] = field(default_factory=list)          # task-graph edges (Step 3)
    tags: str = ""                                          # comma-joined (matches remember()/_tags)
    resolution: str = ""


@runtime_checkable
class LLMClient(Protocol):
    """Provider-agnostic completion + tool-calling. (implemented over an official LLM SDK)
    May optionally expose `is_retryable(error) -> bool` for the retry policy."""
    def complete(self, messages: list[dict], tools: list[dict]) -> AssistantMessage: ...


@runtime_checkable
class ToolHost(Protocol):
    """Executes tools, ideally behind a sandbox. (backed by a container sandbox + MCP tools)"""
    def schemas(self) -> list[dict]: ...
    def run(self, name: str, args: dict) -> str: ...
    def read_text(self, path: str) -> str: ...   # reconstruct the artifacts tier (raises if missing)
    def accesses(self, name: str, args: dict) -> list: ...  # resource accesses for the scheduler


@runtime_checkable
class Retriever(Protocol):
    """Code discovery for the RELATED CODE tier (repo search). (build: ripgrep + tree-sitter)"""
    def retrieve(self, query: str, k: int = 6) -> list[Snippet]: ...


@runtime_checkable
class EvidenceArchive(Protocol):
    """Mandatory L0 episode/evidence compatibility surface."""
    is_durable: bool
    def append_episode(self, session_id: str, task_id: str, turn: int, record: dict) -> None: ...
    def read_episodes(self, session_id: str, *, limit: int | None = None) -> list[dict]: ...
    def search_episodes(self, query: str, *, limit: int = 5, exclude_session: str | None = None,
                        only_session: str | None = None) -> list[dict]: ...


@runtime_checkable
class WorkRepository(Protocol):
    """Mandatory L1 checkpoint compatibility surface while canonical replay lands."""
    def checkpoint_task(self, task: TaskState) -> None: ...
    def load_task(self, task_id: str) -> TaskState | None: ...
    def list_session_tasks(self, session_id: str) -> list[TaskRef]: ...


@runtime_checkable
class KnowledgeStore(Protocol):
    """L2 retrieval/ingestion facade; canonical typed records live in KnowledgeRepository."""
    def recall(self, query: str, k: int = 6, paths: list[str] | None = None) -> list[Snippet]: ...
    def remember(self, content: str, *, title: str = "", scope: str = "default", tags: str = "",
                 paths: list[str] | None = None) -> None: ...
    def mark_used(self, memory_id: str) -> None: ...


@runtime_checkable
class Memory(Protocol):
    """Legacy composite compatibility facade over evidence, work, and knowledge.

    Distinct from Retriever (memem indexes a curated vault, NOT source code). `is_durable` is the
    structural no-op marker: NullMemory sets it False so hosts skip cache/checkpoint wiring (keeps
    evals deterministic). Production uses narrower contracts even while embedding hosts migrate.
    NOTE: @runtime_checkable isinstance() verifies method-NAME presence only — not signatures or
    return types; behavioral fidelity is enforced by the round-trip tests."""
    is_durable: bool
    # --- L2 knowledge compatibility methods (native typed records own production authority) ---
    def recall(self, query: str, k: int = 6, paths: list[str] | None = None) -> list[Snippet]: ...
    def remember(self, content: str, *, title: str = "", scope: str = "default", tags: str = "",
                 paths: list[str] | None = None) -> None: ...
    # --- legacy L0 episodic compatibility mirror (canonical L0 is events + artifact seals) ---
    def append_episode(self, session_id: str, task_id: str, turn: int, record: dict) -> None: ...
    # read side: the model's on-demand valve into the cold cache (recall_history tool). Returns
    # raw line dicts ({v,session_id,task_id,turn,ts,record}); the host renders/bounds them.
    def read_episodes(self, session_id: str, *, limit: int | None = None) -> list[dict]: ...
    # cross-session FTS5 discovery over the rebuildable legacy episode sidecar.
    # Returns bounded hit dicts; [] when the index is unavailable. Single-session reads use
    # read_episodes; this is the ACROSS-sessions counterpart.
    def search_episodes(self, query: str, *, limit: int = 5, exclude_session: str | None = None,
                        only_session: str | None = None) -> list[dict]: ...
    # --- task state / resume ---
    def checkpoint_task(self, task: TaskState) -> None: ...
    def load_task(self, task_id: str) -> TaskState | None: ...
    def list_session_tasks(self, session_id: str) -> list[TaskRef]: ...
    # --- consolidation / retrieval-feedback (declared now; implemented in later steps) ---
    def mark_used(self, memory_id: str) -> None: ...
    # llm = the abstract LLMClient contract (llm-agnostic — never a concrete provider type); returns a
    # stats dict {lessons, skills, skills_rejected, errors}; "skills" counts INACTIVE auto-derived candidates
    # for wire compatibility. Foreground /learn is the only writer that directly activates a skill.
    def consolidate(self, session_id: str, *, llm=None, mode: str = "deterministic") -> dict: ...
    # Release native episode/knowledge indexes. Implementations must make repeated shutdown paths safe.
    def close(self) -> None: ...


@runtime_checkable
class Oracle(Protocol):
    """Ground-truth verification independent of retrieval. (backed by the project's test/lint runners)"""
    def verify(self) -> tuple[bool, str]: ...


def _validate_peer_identity(value: str, field_name: str) -> None:
    """Peer correlation/identity must be a non-empty, single-line, bounded token.

    Rejects EVERY control character (Unicode category C*) and line/paragraph separator
    (U+2028/U+2029) rather than a specific newline byte, so no one-character variant
    (CR, LF, NEL, LS, PS, NUL, ...) can reopen the single-line durable-boundary invariant.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{field_name} must be a non-empty string (<=200 chars)")
    if any(_unicodedata.category(ch)[0] == "C" or ch in "\u2028\u2029" for ch in value):
        raise ValueError(f"{field_name} must be single-line (no control or separator characters)")


@dataclass(frozen=True)
class PeerWait:
    """A durable park on a peer's correlated response (the horizontal analogue of
    waiting_user). Carried as typed Active-Work state, never scraped from prose.

    ``correlation_id`` binds the park to exactly one expected peer result; only a
    matching ``PeerResult.correlation_id`` may resume the request. ``deadline_s`` is
    an optional wall bound after which the wait may be reaped as timed-out.
    """

    correlation_id: str
    peer_id: str = ""
    deadline_s: float | None = None

    def __post_init__(self) -> None:
        _validate_peer_identity(self.correlation_id, "PeerWait.correlation_id")
        _validate_peer_identity(self.peer_id, "PeerWait.peer_id")
        if self.deadline_s is not None:
            import math as _math
            if not isinstance(self.deadline_s, (int, float)) or isinstance(self.deadline_s, bool) \
                    or not _math.isfinite(self.deadline_s) or self.deadline_s < 0:
                raise ValueError("PeerWait.deadline_s must be a finite, non-negative number or None")


@dataclass(frozen=True)
class PeerResult:
    """A correlated reply from a peer that may resume a ``PeerWait``-parked request.

    Resumption is gated on ``correlation_id`` matching the park exactly; a mismatch
    must never silently resume unrelated work.
    """

    correlation_id: str
    peer_id: str = ""
    status: str = "ok"
    report: str = ""

    def __post_init__(self) -> None:
        _validate_peer_identity(self.correlation_id, "PeerResult.correlation_id")
        _validate_peer_identity(self.peer_id, "PeerResult.peer_id")
