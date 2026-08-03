"""Concrete coding-tool host composition for the SliceAgent CLI."""
from __future__ import annotations

import os
import posixpath
import re
import shlex
import stat as _stat
import tempfile

from sliceagent_core import cancel_scope
from sliceagent_core.access import AllAccess, FileAccess
from sliceagent_core.active_work import (
    ActiveWorkError,
    WorkGraph,
    plan_progress_payload,
    build_work_delta,
)
from sliceagent_core.context import ResourceKind, ResourceRef, reserved_resource_ref
from sliceagent_core.contextfs import ContextFS, is_context_path
from sliceagent_core.execution import NO_ADOPT_ON_TIMEOUT_ARG, ToolEffect, ToolStatus
from sliceagent_core.platform_compat import (
    IS_WINDOWS,
    ProcessGroupTerminationError,
    is_win_abs,
    msys_to_win,
    norm_rel,
    win_path_candidates,
)
from sliceagent_core.sensory_cortex import is_ignored as _is_ignored
from sliceagent_core.tool_host import with_note

from .binsniff import looks_binary
from .fuzzy import fuzzy_find_unique
from .procman import ProcManager
from .reach import ReachSet, ReachSteer, SENSITIVE_DIR_NAMES
from .registry import ToolEntry, ToolRegistry, ToolText
from .sandbox import SANDBOX_ADOPTED, SANDBOX_TIMEOUT, LocalSandbox
from .terminal import SessionManager
from .workspace_handoff import WorkspaceScheduleDecision

from .tools import (
    BinaryTextError,
    TOOL_SCHEMAS,
    _CODE_PRELUDE,
    _LEGACY_SEMANTIC_STATE_TOOLS,
    _LIST_CAP,
    _OUTPUT_HEAD,
    _OUTPUT_INLINE_CAP,
    _OUTPUT_TAIL,
    _READ_MAX_LINES,
    _READ_SLURP_CAP,
    _READ_STREAM_CHUNK,
    _coerce_int,
    _default_ask_user,
    _install_signal_cleanup,
    _number_lines,
    _numbered_window,
    _sniff_image_mime,
    _strip_control,
    _strip_line_numbers,
    _unrunnable_verify_program,
    run_item_verification,
)

_SECRET_DIRS = set(SENSITIVE_DIR_NAMES)


class CodingToolHost:
    def __init__(self, root: str | None = None, *, sandbox=None, timeout: int = 120,
                 registry: ToolRegistry | None = None):
        # root=None → confine to the *current* working directory, resolved per call
        # (so the eval runner, which chdirs into a temp workdir after construction,
        # is confined to that workdir). Pass an explicit root to pin it.
        self._root = root
        self.timeout = timeout
        self.sandbox = sandbox or LocalSandbox()
        # Background/long-running processes — the live-handle registry the one-shot sandbox can't
        # express (servers, multi-minute builds). Scrubs secrets like the sandbox; cleanup() at exit.
        _scrub = getattr(self.sandbox, "scrub_secrets", True)
        self.procs = ProcManager(scrub_secrets=_scrub)
        # Interactive PTY sessions — drive REPLs/TUIs/games, hold shell+env across turns.
        self.terminals = SessionManager(scrub_secrets=_scrub)
        # I2 — RE-OBSERVATION REACH = ACTION REACH. File tools and shell must reach the
        # SAME places, or the agent writes (via shell, unconfined) files its file tools can
        # never read back, and OPEN FILES lies "(not created yet)" about real on-disk files.
        # The workspace is the default frame, not a prison. ReachSet keeps it distinct from grounded
        # external focus roots while preserving one path capability for every path-aware tool.
        self._reach = ReachSet(lambda: self._root or os.getcwd())
        # Permanent cognitive address space. Runtime providers arrive later; the root/status surface itself is
        # always truthful and independent of optional semantic-memory backends.
        self._contextfs = ContextFS()
        # The read-only VIRTUAL `history/` namespace (this session's sealed turns as files). Injected by the
        # CLI (a HistoryFS) once memory+session exist; None on the eval/headless path (no durable archive).
        self._history = None
        # Whole-file overwrite guard: (mtime_ns, size) per file at read_file time — a write that
        # finds the file changed underneath is refused (see _stale_write_guard).
        self._read_marks: dict = {}
        self._artifacts = None  # authoritative local turn/subagent artifacts (always-on in the CLI)
        self._subagents = None   # a SubagentFS (subagents/ virtual namespace) — the parent's view of child seals
        # ask_user (the "come back and ask" capability): a host callback that prompts the real user and
        # returns their answer. Defaults to a non-interactive fallback so headless/eval never hangs; the
        # CLI overrides it with a TUI/plain prompt. Injected (not a core dependency) — task/LLM-agnostic.
        self.on_ask_user = _default_ask_user
        # Host control-plane callback. The tool only REQUESTS a workspace-runtime handoff; the CLI performs it
        # after a successful durable turn seal. None in tests/embedded hosts = unsupported.
        self.on_workspace_switch = None
        # Read-only provider used to validate update_work against the active graph before an effect is emitted.
        # It returns (WorkGraph, logical_turn_id, workspace_epoch); the reducer remains the sole mutator.
        self._active_work_provider = None
        # P2 item verification: injectable runner (tests), one-shot green memo consumed by the effect
        # factory, and the per-item failure-signature history for oscillation detection.
        self._verify_runner = None
        self._verify_notify = None      # optional presentation callback: announces each verify command
        self._item_verify_green: dict = {}
        self._verify_attempts: dict = {}
        self._efficiency_metrics = {
            "result_repeat_count": 0,
            "result_repeat_source_chars": 0,
            "result_alias_count": 0,
            "result_alias_source_chars": 0,
            "result_alias_inline_chars": 0,
        }
        self._edit_journal: list = []   # (rel, full, prev_bytes|None) per write — powers /undo
        self.pending_images: list = []  # images @-attached for the NEXT seed build (vision models only)
        # The registry is the single source of tools; MCP/plugin/skill tools register
        # into this same object later (Step ③). The host just projects from it.
        self.registry = registry or ToolRegistry()
        self._register_builtins()
        import atexit
        self._closed = False
        self._atexit_cleanup = self.cleanup
        atexit.register(self._atexit_cleanup)  # leaked background procs / PTYs must not survive exit/abort/crash
        # The SIGNAL path gets the bounded variant: a supervisor's SIGKILL deadline leaves no room
        # for the default per-child graces.
        self._signal_cleanup = lambda: self.cleanup(bounded=True)
        _install_signal_cleanup(self._signal_cleanup)


    def cleanup(self, *, bounded: bool = False) -> None:
        """Tear down background processes + PTY sessions (idempotent; never raises). Wired to atexit AND
        called by the CLI on exit/abort, so leaked servers/shells/PTYs don't outlive the agent (#5).

        ``bounded=True`` (the SIGNAL path): the sweep runs under a supervisor's SIGKILL deadline
        (docker stop gives ~10s), so per-child graces shrink to 1.0s/0.5s — the default 3s+2s per
        SIGTERM-ignoring child measured 9.22s for three, which is what provokes the second signal
        (and a mid-sweep SIGKILL). In-flight FOREGROUND commands are reaped first: their Popen
        handles live only inside sandbox._exec, so without this reach a SIGTERM during a build
        orphaned the whole group (the review's U8 foreground finding)."""
        if self._closed:
            return
        self._closed = True
        # In-process workspace switches create a replacement host. Retaining every retired host through its
        # bound atexit callback would leak the full registry/session graph until process exit.
        try:
            import atexit
            atexit.unregister(self._atexit_cleanup)
        except Exception:
            pass
        reap = getattr(self.sandbox, "reap_inflight", None)
        if callable(reap):
            try:
                reap()
            except Exception:  # noqa: BLE001 — the sweep must never delay the exit
                pass
        for _mgr in (getattr(self, "procs", None), getattr(self, "terminals", None)):
            try:
                if _mgr is None:
                    continue
                if bounded and _mgr is getattr(self, "procs", None):
                    _mgr.cleanup(term_grace=1.0, kill_grace=0.5)
                else:
                    _mgr.cleanup()
            except Exception:  # noqa: BLE001
                pass

    def _register_builtins(self) -> None:
        handlers = {
            "read_file": self._t_read_file, "list_files": self._t_list_files,
            "change_workspace": self._t_change_workspace,
            "edit_file": self._t_edit_file, "append_to_file": self._t_append,
            "str_replace": self._t_str_replace, "run_command": self._t_run_command,
            "execute_code": self._t_execute_code, "ask_user": self._t_ask_user,
            "proc_start": self._t_proc_start, "proc_poll": self._t_proc_poll,
            "proc_tail": self._t_proc_tail, "proc_wait": self._t_proc_wait,
            "proc_kill": self._t_proc_kill,
            "terminal_open": self._t_terminal_open, "terminal_send": self._t_terminal_send,
            "terminal_read": self._t_terminal_read, "terminal_wait": self._t_terminal_wait,
            "terminal_close": self._t_terminal_close,
            "world_set": self._t_world_set, "world_clear": self._t_world_clear,
            "reconcile_execution": self._t_reconcile_execution,
            "require": self._t_require, "requirement_done": self._t_requirement_done,
            "supersede_requirement": self._t_supersede_requirement,
            "drop_requirement": self._t_drop_requirement,
            "update_work": self._t_update_work,
            "code_review": self._t_code_review,
        }
        for schema in TOOL_SCHEMAS:
            name = schema["function"]["name"]
            self.registry.register(ToolEntry(
                name=name, schema=schema, handler=handlers[name],
                accesses=(lambda args, n=name: self._builtin_accesses(n, args)),
                source="builtin",
                capabilities=(frozenset({"workspace_handoff"}) if name == "change_workspace" else frozenset()),
                effect_factory=(
                    self._read_resource_effects if name == "read_file"
                    else self._work_delta_effects if name == "update_work"
                    else None
                ),
            ))

    def bind_active_work(self, provider) -> None:
        """Bind the current application task without giving the tool host mutation ownership."""
        self._active_work_provider = provider

    def _active_work_snapshot(self) -> tuple[WorkGraph, str, int]:
        if not callable(self._active_work_provider):
            raise ValueError("ACTIVE WORK is unavailable in this host")
        graph, logical_id, workspace_epoch = self._active_work_provider()
        if not isinstance(graph, WorkGraph):
            raise ValueError("ACTIVE WORK provider returned no graph")
        return graph, str(logical_id or ""), int(workspace_epoch)

    def _work_delta_effects(self, invocation, status, _text) -> tuple[ToolEffect, ...]:
        if status is not ToolStatus.SUCCEEDED:
            return ()
        graph, logical_id, workspace_epoch = self._active_work_snapshot()
        delta = build_work_delta(
            graph, dict(invocation.args), logical_id=logical_id, workspace_epoch=workspace_epoch,
            verified_ok=frozenset(self._item_verify_green),
        )
        next_graph = graph.apply_delta(delta)
        return (ToolEffect(
            id=f"work-delta:{invocation.provider_index}:{invocation.id}:0",
            kind="work_delta", payload={
                "delta": delta.to_dict(),
                "plan_progress": plan_progress_payload(next_graph, logical_id),
            },
        ),)

    def root(self) -> str:
        return self._reach.primary

    def preserve_observation_result(self, _name: str, _args: dict, text: str) -> str | None:
        """Persist one exact T4 observation and return its model-readable locator.

        This is deliberately a host capability rather than core filesystem I/O. A failure returns ``None``;
        the core arm then keeps the full result inline, so losslessness is the gate rather than an aspiration.
        """
        import hashlib
        content = str(text or "")
        if not content:
            return None
        digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        rel = f".sliceagent/blobs/observation-{digest}.txt"
        try:
            full = self._resolve(rel)
            self._mkparent(full)
            if os.path.exists(full):
                with open(full, encoding="utf-8", newline="") as existing:
                    if existing.read() != content:
                        return None
            else:
                self._atomic_write(full, content)
            return rel
        except Exception:
            return None

    def record_result_alias(self, *, source_chars: int, inline_chars: int) -> None:
        """Host-private T4 counters used by the paired A/B differential table."""
        self._efficiency_metrics["result_alias_count"] += 1
        self._efficiency_metrics["result_alias_source_chars"] += max(0, int(source_chars))
        self._efficiency_metrics["result_alias_inline_chars"] += max(0, int(inline_chars))

    def record_result_repeat(self, *, source_chars: int) -> None:
        """Counter-only T4 stratum marker; it changes no provider-visible control-arm bytes."""
        self._efficiency_metrics["result_repeat_count"] += 1
        self._efficiency_metrics["result_repeat_source_chars"] += max(0, int(source_chars))

    def efficiency_metrics(self) -> dict[str, int]:
        out = dict(self._efficiency_metrics)
        out["result_alias_saved_chars"] = max(
            0, out["result_alias_source_chars"] - out["result_alias_inline_chars"],
        )
        return out

    # Compatibility projections for older embedding hosts/tests. ReachSet remains the sole owner.
    @property
    def _extra_roots(self) -> list[str]:
        return list(self._reach.focus_roots)

    @property
    def _focus(self) -> str | None:
        return self._reach.active_focus

    @_focus.setter
    def _focus(self, path: str | None) -> None:
        self._reach.active_focus = path

    def add_root(self, path: str) -> str | None:
        """Mark a directory the goal/user EXPLICITLY targets as in-reach for file tools.

        The minimal, safe, task-agnostic mechanism for "explicitly-targeted dir" (I2): a
        SETTABLE root, not goal-parsing heuristics. After this, read_file/edit_file/list_files
        resolve paths under `path` exactly as the shell already does (shell is unconfined),
        so a shell-written file is always readable back through OPEN FILES — reach matches.
        Refuses a blanket root ('/' or '~') so grounded reach cannot become ambient home/system access.
        Returns the realpath added (idempotent), or None if rejected/unusable."""
        if not path:
            return None
        return self._reach.add(path, source="explicit")

    def allowed_roots(self) -> list[str]:
        """The set of dirs file tools may reach: the primary project ∪ grounded focus roots.
        Honored by `_resolve`; matches where the shell already acts (I2: reach = action reach)."""
        return list(self._reach.roots)

    def focus(self) -> tuple[str | None, list[str]]:
        """The active focus (most-recently-worked EXTERNAL dir) + every extra root the file tools reach
        beyond the workspace. Surfaced in the slice so the model KNOWS its file tools reach there: the
        auto-granted reach was invisible, so the agent defaulted to the workspace frame and lost the
        thread across turns (the hunter 'index.ts' miss). Delegated by ScopedSpawnHost via __getattr__."""
        return self._reach.active_focus, list(self._reach.focus_roots)

    def resolution_base(self) -> str:
        """The CURRENT PROJECT a bare RELATIVE path resolves against — the frame, not the floor. Defaults
        to the active focus (the most-recent dir worked in) when set, else the primary root. This ONLY
        moves the relative-path anchor + display frame; it NEVER widens reach: the result of `_resolve`
        must still land inside `allowed_roots()`, and the primary root is unchanged. So the
        working frame can move among grounded roots without silently widening the floor."""
        base = self._reach.active_focus or self.root()
        # defensive: the base must itself be a reachable root (focus is only ever set to a granted dir)
        return base if base in self.allowed_roots() else self.root()

    def locate(self, path: str) -> str:
        """Resolve a working-set path for RE-READING (OPEN FILES). Base-STABLE — independent of the current
        project: a relative path is matched against EVERY reachable root (boundary root first, then extra
        roots) and the first EXISTING match wins, so a pin stays truthful even after `resolution_base()`
        moves. Falls back to the boundary-root resolution when nothing exists, so the truthful
        '(not created yet)' / 'outside reach' branch in build_artifacts still fires per exception type."""
        expanded = os.path.expanduser(path)
        if os.path.isabs(expanded):
            return self._resolve(path)                       # absolute → _resolve enforces the boundary
        for r in self.allowed_roots():
            cand = os.path.realpath(os.path.join(r, expanded))
            if (cand == r or cand.startswith(r + os.sep)) and os.path.exists(cand):
                return cand
        # nothing exists under any root → a boundary-SAFE truthful-404 path. realpath + confine so a relative
        # '../x' can't resolve to a real file OUTSIDE the boundary when read_file opens it (confinement).
        root = self.root()
        fallback = os.path.realpath(os.path.join(root, expanded))
        if fallback == root or fallback.startswith(root + os.sep):
            return fallback
        return self._resolve(path)                           # escapes the boundary → raise (same as the file tools)

    def _grant_shell_paths(self, text: str) -> None:
        """I2 — reach FOLLOWS action. When the shell acts on a path outside the allowed roots,
        grant file-tool reach to its directory so a shell-written file is ALWAYS readable back via
        OPEN FILES. No NEW capability — the shell already reaches there; this only lets the file
        tools observe it (the original split-brain: writes it could never read back). Restricted to
        the user's HOME subtree, never HOME itself or an ancestor of the workspace (add_root also
        refuses '/' and '~'). Pure path detection — task/LLM-agnostic, no command parsing."""
        if not text:
            return
        home = os.path.realpath(os.path.expanduser("~"))
        root = self.root()
        # quoted paths (may contain spaces) OR bare ~/-rooted tokens up to a shell metachar/space
        cands = [(q or uq).strip() for q, uq in re.findall(
                r"""['"]([^'"]*/[^'"]*)['"]|(?<![\w'"])((?:~|/)[^\s'"|&;<>()]+)""", text)]
        if IS_WINDOWS:
            # win32 (Git Bash): commands carry 'C:\x' / "C:/x" / bare C:\x tokens the POSIX
            # extractor can't see, plus MSYS '/c/x' mounts. Seam logic in platform_compat.
            cands = [msys_to_win(c) for c in cands] + win_path_candidates(text)
        for cand in cands:
            if not (cand.startswith("/") or cand.startswith("~")
                    or (IS_WINDOWS and is_win_abs(cand))):
                continue
            # H4: drop version-shaped tokens ('/v1.2.3', '/1.0') — a coincidental '/'-run from a version
            # string, not a path the command operates on. (The must-be-an-existing-dir-UNDER-HOME guards
            # below already exclude nearly all false positives; this kills the named residual class before
            # even touching the filesystem.)
            if re.fullmatch(r"[/~]v?\d[\d.]*", cand):
                continue
            full = os.path.realpath(os.path.expanduser(cand))
            d = full if os.path.isdir(full) else os.path.dirname(full)
            if not d or not os.path.isdir(d):
                continue
            if not d.startswith(home + os.sep):          # only the user's own subtree (excludes HOME itself)
                continue
            if d == root or root.startswith(d + os.sep):  # never an ancestor of the workspace
                continue
            # #31: never auto-widen file-tool reach into credential/secret dirs, even inside HOME — a path
            # merely MENTIONED in an allowed shell command must not make ~/.ssh etc. readable by the tools.
            if any(part.lower() in _SECRET_DIRS for part in d.split(os.sep)):   # casefold: ~/.SSH == ~/.ssh on a case-insensitive FS (macOS)
                continue
            self.add_root(d)
            self._focus = d   # the most-recent external dir the shell worked on → the active focus

    def resolve_read(self, path: str) -> str:
        """Resolution shared by read_file AND the OPEN FILES display so they never diverge. Prefer the
        current-project (focus) copy; if nothing exists there, fall back to a base-STABLE search of every
        reachable root (locate). Keeps focus-relative semantics while making a paged-out blob — or any file
        under a root that isn't the current focus — reachable regardless of where focus now points (the
        blob's read_file('.sliceagent/blobs/…') ref was minted against a possibly-different base)."""
        try:
            full = self._resolve(path)
        except PermissionError:
            # An exact absolute target below HOME is enough to grant the narrow containing directory for
            # ordinary observation/work. This removes the shell-vs-file split without admitting HOME or
            # credential directories. Relative traversal and system paths remain outside automatic reach.
            if self._reach.observation_root(path) or self._reach.target_root(path):
                full = self._resolve(path)
            else:
                return self.locate(path)
        except ValueError:
            return self.locate(path)
        if os.path.exists(full):
            return full
        alt = self.locate(path)
        return alt if os.path.exists(alt) else full

    def _archive_handle(self, path: str) -> str:
        """Canonical model-visible handle for a reserved archive path.

        The model may spell a virtual handle either as ``artifacts/x.md`` or as the equivalent absolute
        path below a reachable root (for example ``/workspace/artifacts/x.md``). Archive filesystems are
        intentionally unaware of physical roots, so collapse the latter spelling back to the same relative
        handle before dispatch. Absolute paths outside every reachable root stay absolute and therefore
        cannot acquire virtual-archive meaning.
        """
        raw = str(path or "").strip()
        expanded = os.path.expanduser(raw)
        if os.path.isabs(expanded):
            full = os.path.realpath(expanded)
            # Prefer the most-specific reachable root when roots are nested: the archive mount is relative
            # to the root that directly owns it, not an ancestor that happens to contain that root.
            roots = sorted((os.path.realpath(root) for root in self.allowed_roots()),
                           key=len, reverse=True)
            for root in roots:
                if full == root or full.startswith(root + os.sep):
                    raw = os.path.relpath(full, root)
                    break
            else:
                raw = full
        normalized = posixpath.normpath(raw.replace("\\", "/"))
        return normalized.rstrip("/") or "."

    def _history_route(self, path):
        """Return the virtual FS (HistoryFS for `history/`, SubagentFS for `subagents/`) iff `path` targets that
        reserved namespace AND no real on-disk file shadows it — a real file/dir ALWAYS wins the name (I2: the
        virtual view never lies about disk). Else None. ponytail: these are reserved virtual namespaces; a
        project with a real top-level history/ or subagents/ dir keeps its files (real wins). Absolute paths
        under a reachable root are first collapsed to their model-visible archive handle. Used by
        read_file/list_files/grep to route reads, and by the write tools to reject (a virtual route ⇒ read-only)."""
        if is_context_path(path):
            return self._contextfs
        p = self._archive_handle(path)
        for mount, fs in (("artifacts", self._artifacts), ("history", self._history),
                          ("subagents", self._subagents)):
            if fs is None or not (p == mount or p.startswith(mount + "/")):
                continue
            try:
                real = self.resolve_read(path)
            except (ValueError, PermissionError):
                real = None
            return None if (real and os.path.exists(real)) else fs
        return None

    def resource_ref(self, path: str) -> ResourceRef:
        """Return the actual resource addressed by ``path`` on this host.

        Reserved archive mounts are virtual only when no real project path shadows them.  This is the
        classification seam shared by execution effects and slice reconstruction, so an artifact can never
        silently become an ``OPEN FILES`` workspace path (or vice versa).
        """
        # A physical workspace handle is the spelling the file tools actually resolved.  The reserved-resource
        # classifier normalizes backslashes because virtual handles are POSIX-shaped, but applying that
        # normalization to a real Windows path makes execution provenance name a different handle than the
        # invocation.  Keep virtual handles canonical and physical handles native.
        physical_ref = ResourceRef(ResourceKind.WORKSPACE_FILE, str(path) if path else ".")
        ref = reserved_resource_ref(self._archive_handle(path))
        if ref.kind is ResourceKind.WORKSPACE_FILE:
            return physical_ref
        return (ref if self._history_route(path) is not None
                else physical_ref)

    def _read_resource_effects(self, invocation, status, _text) -> tuple[ToolEffect, ...]:
        """Attach the read's resource kind to canonical execution truth."""
        if status is not ToolStatus.SUCCEEDED:
            return ()
        import hashlib
        from sliceagent_core.fan_in import artifact_read_coverage, artifact_view_kind, canonical_artifact_id

        ref = self.resource_ref(str(invocation.args.get("path") or ""))
        payload = {"resource_kind": ref.kind.value, "handle": ref.handle}
        content = str(_text or "")
        artifact_id = canonical_artifact_id(ref.kind, ref.handle)
        if ref.kind is ResourceKind.SUBAGENT:
            # A named specialist handle is an alias. The rendered immutable report leads with its exact
            # per-job id, so consumption joins to the seal rather than to the mutable alias spelling.
            exact = re.match(r"^# (sub-\d+) —", content)
            if exact:
                artifact_id = exact.group(1)
        if artifact_id:
            artifact_view = artifact_view_kind(ref.kind, ref.handle)
            payload.update({
                "artifact_id": artifact_id,
                "artifact_view": artifact_view,
                "read_coverage": artifact_read_coverage(
                    invocation.args, content, resource_kind=ref.kind, handle=ref.handle,
                ),
                "content_sha256": hashlib.sha256(content.encode("utf-8", "replace")).hexdigest(),
                "content_bytes": len(content.encode("utf-8", "replace")),
            })
        return (ToolEffect(
            id=f"resource:{invocation.provider_index}:{invocation.id}:0",
            kind="resource_observed",
            payload=payload,
        ),)

    def _history_readonly_guard(self, path):
        """ToolText rejecting a WRITE to a virtual namespace (history/ or subagents/ — read-only views of the
        sealed archive); None when the path isn't virtual (real files/dirs write normally)."""
        fs = self._history_route(path)
        if fs is None:
            return None
        what = ("@sliceagent/ is the read-only internal context namespace"
                if fs is self._contextfs else
                "artifacts/ is the read-only authoritative local artifact archive"
                if fs is self._artifacts else
                "subagents/ is a read-only view of your subagents' sealed reports"
                if fs is self._subagents else
                "history/ is a read-only view of this session's past turns (the episodic archive)")
        return ToolText(
            f"{what} — you can read_file/list_files/grep it, but it can't be written. Save work elsewhere.",
            status=ToolStatus.STEERED,
        )

    def _resolve(self, path: str) -> str:
        """Resolve a tool path under an ALLOWED root (workspace ∪ explicitly-targeted dirs);
        reject escapes. expanduser FIRST so '~' behaves like the shell (P2) instead of
        silently creating a literal '~' dir inside the workspace."""
        if not path:
            raise ValueError("empty path")
        path = os.path.expanduser(path)  # P2 — '~' → $HOME before any join/realpath
        roots = self.allowed_roots()
        # A bare relative path resolves against the CURRENT PROJECT (resolution_base), not always the
        # boundary root — so when the agent moves into another reachable project, relative paths follow
        # it. Reach is unchanged: `full` must still land inside a reachable root below.
        base = self.resolution_base()
        full = path if os.path.isabs(path) else os.path.join(base, path)
        full = os.path.realpath(full)
        for root in roots:
            if full == root or full.startswith(root + os.sep):
                return full
        # P3 — prescriptive error: name the boundary AND the escape hatch so a no-transcript
        # model recovers instead of re-deriving the dead end (and looping into shell fallback).
        raise ReachSteer(
            f"path is outside the current workspace and grounded focus roots ({base}): {path}. "
            "Use the exact absolute target under your home directory, use run_command for a deliberately named "
            "system path, or call change_workspace(path) to make another project primary; the interface and "
            "model stay connected.")

    def _resolve_for_access(self, path: str) -> str | None:
        """Canonical PHYSICAL path for SCHEDULING conflict detection only — NOT a security check (the real
        _resolve enforces the boundary at run time). Mirrors _resolve's expanduser + base-join + realpath
        so 'foo.py', './foo.py', and the absolute spelling collapse to ONE key, and the scheduler then
        serializes concurrent writes to the same inode (otherwise a parallel edit_file + str_replace via
        different spellings race → lost update). Returns None on empty/bad input → caller falls back."""
        if not path:
            return None
        try:
            p = os.path.expanduser(path)
            base = self.resolution_base()
            full = p if os.path.isabs(p) else os.path.join(base, p)
            return os.path.realpath(full)
        except Exception:  # noqa: BLE001 — access declaration must never fail the call
            return None

    # --- ToolHost projection: everything comes from the registry now ---
    def schemas(self) -> list[dict]:
        # inject the 'note' arg into every tool so the model's per-turn conclusion rides on the
        # call it already makes and lands in the slice's FINDINGS tier (anti-re-derivation)
        schemas = self.registry.schemas()
        if callable(self._active_work_provider):
            # Active Work is the sole semantic state API in the new kernel.  Hiding the old requirement/plan/
            # world scratchpads and their generic note arg removes seven competing ways to describe the same
            # task.  Registry entries remain executable for old checkpoints/embedding hosts but are not offered
            # to the production model once the application graph is bound.
            return [
                schema for schema in schemas
                if schema.get("function", {}).get("name") not in _LEGACY_SEMANTIC_STATE_TOOLS
            ]
        return [with_note(schema) for schema in schemas]

    def accesses(self, name: str, args: dict) -> list:
        return self.registry.accesses(name, args)

    def run(self, name: str, args: dict) -> str:
        return self.registry.run(name, args)  # registry wraps the handler in try/except

    def preflight_run(self, name: str, args: dict):
        """Return one registry admission for the scheduler's truthful start boundary."""
        return self.registry.admit(name, args)

    def run_preflighted(self, name: str, args: dict, admission) -> str:
        """Execute the exact entry admitted before ``ToolStarted`` without a volatile second check."""
        if getattr(admission, "name", None) != name:
            from .registry import ToolText
            return ToolText("Error: tool admission does not match invocation", ok=False)
        return self.registry.run_admitted(admission, args)

    def read_text(self, path: str, *, lossy: bool = True) -> str:
        # Read bytes first so the binary gate runs BEFORE we trust the file as text.
        # A NUL byte / mostly-control-char head means "not text" — feeding it through
        # OPEN FILES would corrupt the slice and burn tokens. ValueError flows through
        # the registry try/except so both read_file and str_replace degrade gracefully.
        full = self.resolve_read(path) if lossy else self._resolve(path)
        with open(full, "rb") as f:
            raw = f.read()
        sample = raw[:8192].decode("utf-8", errors="replace")
        if looks_binary(path, sample):
            raise BinaryTextError(f"{path} appears to be binary; not shown")
        # DISPLAY callers (read_file / OPEN FILES render) pass lossy=True: a stray invalid UTF-8 byte PAST
        # the 8192-byte sniff sample must not crash an otherwise-text file's read. The READ-MODIFY-WRITE
        # caller (str_replace) passes lossy=False: strict decode RAISES on any invalid byte so the call
        # aborts cleanly (file untouched) instead of writing back a U+FFFD-mangled whole file — silent
        # corruption of bytes the edit never touched.
        return raw.decode("utf-8", errors="replace" if lossy else "strict")

    def _builtin_accesses(self, name: str, args: dict) -> list:
        """Declare what each builtin call touches so the scheduler can safely parallelize."""
        p = args.get("path")
        # resolve to the physical path so two spellings of one file conflict (and serialize) correctly
        if name == "read_file":
            rp = self._resolve_for_access(p)
            return [FileAccess("read", rp)] if rp else []
        if name == "list_files":
            d = args.get("path") or "."
            return [FileAccess("search", self._resolve_for_access(d) or d, recursive=True)]
        if name in ("edit_file", "append_to_file", "str_replace"):
            rp = self._resolve_for_access(p)
            return [FileAccess("readwrite", rp)] if rp else [AllAccess()]
        if name in ("run_command", "execute_code", "proc_start", "proc_poll",
                    "proc_tail", "proc_wait", "proc_kill", "terminal_open", "terminal_send",
                    "terminal_read", "terminal_wait", "terminal_close"):
            return [AllAccess()]  # arbitrary / stateful execution → globally exclusive
        return [AllAccess()]

    # --- builtin tool handlers (args) -> str (the registry catches exceptions) ---
    def _t_change_workspace(self, args: dict) -> str:
        """Request an atomic workspace-resource handoff; never partially reroot this live host."""
        raw = str(args.get("path") or "").strip()
        if not raw or "\x00" in raw:
            return ToolText("Error: change_workspace requires a valid directory path.", ok=False)
        try:
            expanded = os.path.expanduser(raw)
            target = os.path.realpath(
                expanded if os.path.isabs(expanded) else os.path.join(self.root(), expanded)
            )
        except (OSError, ValueError):
            return ToolText(f"Error: not a directory: {raw}", ok=False)
        if not os.path.isdir(target):
            return ToolText(f"Error: not a directory: {raw}", ok=False)
        if target == self.root():
            return f"Workspace already active: {target}"
        if self.on_workspace_switch is None:
            return ToolText("Error: this host does not support workspace handoff.", ok=False)
        decision = self.on_workspace_switch(target)
        if isinstance(decision, WorkspaceScheduleDecision):
            if not decision.accepted:
                return ToolText(decision.message, status=decision.status)
        elif decision:
            # Compatibility for embedding hosts that still implement the historical
            # ``"" on success, problem string on failure`` callback.
            return ToolText(f"Error: {decision}", ok=False)
        return (
            f"Workspace switch scheduled: {target}. The host will save this turn and atomically activate the "
            "new workspace while keeping the interface and model connection alive. Do not call more tools; "
            "finish this response now."
        )

    def _page_out(self, text: str, *, label: str = "output") -> str:
        """Page a large tool output OUT to a blob and return a BOUNDED head+tail view + a read_file
        reference, instead of inlining the whole thing into the turn transcript. Moat-coherent: the FULL
        output is preserved on disk (recall-on-demand, the L1→L2 page-out), never cut. Best-effort — on a
        write failure it still bounds the inline view with a hard head+tail slice."""
        if not text or len(text) <= _OUTPUT_INLINE_CAP:
            return _strip_control(text)   # strip C0/NUL on the SMALL path too — a NUL is valid UTF-8 (errors='replace' won't drop it) and breaks the LLM JSON request
        text = _strip_control(text)   # paged path: plain-text blob (read_file page-back works) + API-safe view
        if len(text) <= _OUTPUT_INLINE_CAP:
            # control-heavy output can drop below the cap AFTER stripping — return it inline rather than
            # computing head/tail/elided on the now-short text (which gave a negative elided + duplicated
            # head==tail content + a false "paged out" banner). The full clean output still rides the turn.
            return text
        ref = None
        try:
            import hashlib
            digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
            rel = f".sliceagent/blobs/{label.replace(' ', '-')}-{digest}.txt"   # forward slashes on BOTH platforms: the model-visible ref must match the bash-flavored tool contract (and Windows file APIs accept '/')
            full = self._resolve(rel)
            self._mkparent(full)
            if not os.path.exists(full):
                self._atomic_write(full, text)
            ref = f"read_file('{rel}')"
        except Exception:  # noqa: BLE001 — a paging failure must never fail the tool itself
            ref = None
        elided = len(text) - _OUTPUT_HEAD - _OUTPUT_TAIL
        how = f"page the full {label} back with {ref}" if ref else f"the elided {label} is unavailable (blob write failed)"
        return (f"{text[:_OUTPUT_HEAD]}\n\n"
                f"[… {elided} of {len(text)} chars paged out — {how} …]\n\n"
                f"{text[-_OUTPUT_TAIL:]}")

    def _t_read_file(self, args: dict) -> str:
        # Text files: return the content. Binary files: instead of refusing (which blanks the
        # agent on forensics/media/archive tasks), return a hexdump + size + magic so it can
        # inspect structure and pick the right CLI. str_replace still uses read_text() (which
        # raises on binary) — you can't text-edit a binary, so that path stays a hard error.
        path = args["path"]
        hf = self._history_route(path)
        if hf is not None:               # read-only VIRTUAL history/ (this session's sealed turns as files)
            return hf.read_file(self._archive_handle(path))
        full = self.resolve_read(path)   # focus copy if present, else search all roots (paged-out blob recall)
        try:
            st = os.stat(full)
        except OSError as e:
            raise FileNotFoundError(str(e)) from e
        if not _stat.S_ISREG(st.st_mode):
            # A FIFO/device/socket never EOFs: a plain read() wedges the turn forever and burns a
            # reader slot with it (the review's D1). Redirect instead of blocking.
            kind = ("a FIFO/pipe" if _stat.S_ISFIFO(st.st_mode) else
                    "a device" if _stat.S_ISCHR(st.st_mode) or _stat.S_ISBLK(st.st_mode) else
                    "a socket" if _stat.S_ISSOCK(st.st_mode) else "not a regular file")
            return ToolText(
                f"read_file: {path} is {kind} — reading it would block forever. Inspect it with "
                "run_command/execute_code under a timeout (e.g. dd/cat with a bound) instead.",
                status=ToolStatus.STEERED)
        offset, limit = _coerce_int(args.get("offset")), _coerce_int(args.get("limit"))
        if st.st_size > _READ_SLURP_CAP:
            return self._huge_file_view(path, full, st, offset, limit)
        with open(full, "rb") as f:
            raw = f.read()
        self._mark_read(full)
        sample = raw[:8192].decode("utf-8", errors="replace")
        if looks_binary(path, sample):
            return self._binary_view(path, raw)
        # Return WITH cat -n line numbers so the model has file:line evidence immediately this turn (matching
        # the OPEN FILES render). Safe for editing: str_replace strips a pasted line-number prefix.
        # BOUNDED VIEW (moat-safe): a huge file would flood the slice, so cap the default view + support a
        # line window (offset/limit). The FULL file always stays on disk — this bounds the VIEW, not the file.
        lines = raw.decode("utf-8", errors="replace").splitlines()   # consistent with read_text's gate decode
        total = len(lines)
        windowed = offset is not None or limit is not None
        # a paged-out blob recall is the deliberate L1→L2 "give me the FULL output back" channel — never cap
        # it (only the default view of an ordinary file is capped). Still windowable if offset/limit is given.
        is_blob = ".sliceagent/blobs/" in path.replace("\\", "/") or ".sliceagent/blobs/" in str(full).replace("\\", "/")
        if not windowed:
            start, end = 1, (total if (is_blob or total <= _READ_MAX_LINES) else _READ_MAX_LINES)
        else:
            start = min(max(1, offset or 1), total + 1)
            end = total if limit is None else min(total, start - 1 + max(1, limit))
        body = _number_lines(lines[start - 1:end], start)
        if not windowed and end >= total:
            return body                                  # complete read → unchanged contract (no footer)
        more = (f" · +{total - end} more — read_file(path, offset={end + 1}) to continue"
                if end < total else "")
        return f"{body}\n<system>read_file {path}: lines {start}-{end} of {total}{more}</system>"

    def _huge_file_view(self, path: str, full: str, st, offset, limit) -> str:
        """Memory-bounded view for files above _READ_SLURP_CAP (the review's G2: a 159MB file cost
        ~700MB RSS to show 65KB; a 1GiB log ~2.4GB). Total lines are counted in one streaming pass
        and only the requested window is ever materialized — the contract (line numbers, footer,
        offset/limit paging) is identical to the small-file path."""
        self._mark_read(full)
        with open(full, "rb") as f:
            sample = f.read(8192).decode("utf-8", errors="replace")
        if looks_binary(path, sample):
            with open(full, "rb") as f:
                head = f.read(4096)
            return (f"{path}: binary file, {st.st_size:,} bytes — text tools can't edit it; "
                    f"inspect/convert it with run_command/execute_code (the right CLI).\n"
                    f"magic: {head[:8].hex()}\nhexdump (first 256 bytes):\n"
                    + "\n".join(self._hexrows(head[:256])))
        total = 0
        last_byte = b""
        with open(full, "rb") as f:
            for chunk in iter(lambda: f.read(_READ_STREAM_CHUNK), b""):
                total += chunk.count(b"\n")
                last_byte = chunk[-1:]
        # splitlines() semantics (the small-file path's counter): a final line WITHOUT a trailing
        # newline is still a line — a jq -c dump, a minified bundle, a single-line JSON blob.
        # Without this the total was one short and _stream_line_window's end was clamped below
        # the last line, silently dropping it from the view (the review's U5).
        if last_byte and last_byte != b"\n":
            total += 1
        windowed = offset is not None or limit is not None
        if not windowed:
            start, end = 1, min(total, _READ_MAX_LINES)
        else:
            start = min(max(1, offset or 1), total + 1)
            end = total if limit is None else min(total, start - 1 + max(1, limit))
        lines = self._stream_line_window(full, start, end)
        body = _number_lines(lines, start)
        more = (f" · +{total - end} more — read_file(path, offset={end + 1}) to continue"
                if end < total else "")
        return (f"{body}\n<system>read_file {path}: lines {start}-{end} of {total} "
                f"({st.st_size:,} bytes; memory-bounded streaming read){more}</system>")

    @staticmethod
    def _stream_line_window(full: str, start: int, end: int) -> list[str]:
        """Lines [start, end] (1-based, inclusive) read with bounded memory — never slurps."""
        out: list[str] = []
        if start > end:
            return out
        lineno = 0
        with open(full, "rb") as f:
            pending = b""
            for chunk in iter(lambda: f.read(_READ_STREAM_CHUNK), b""):
                rows = (pending + chunk).split(b"\n")
                pending = rows.pop()
                for raw_line in rows:
                    lineno += 1
                    if start <= lineno <= end:
                        out.append(raw_line.decode("utf-8", errors="replace"))
                    if lineno > end:
                        return out
        if pending:
            lineno += 1
            if start <= lineno <= end:
                out.append(pending.decode("utf-8", errors="replace"))
        return out

    @staticmethod
    def _binary_view(path: str, raw: bytes, head_bytes: int = 256) -> str:
        head = raw[:head_bytes]
        return (f"{path}: binary file, {len(raw)} bytes — text tools can't edit it; inspect/convert "
                f"it with run_command/execute_code (the right CLI).\n"
                f"magic: {head[:8].hex()}\n"
                f"hexdump (first {len(head)} bytes):\n" + "\n".join(CodingToolHost._hexrows(head)))

    @staticmethod
    def _hexrows(head: bytes) -> list[str]:
        rows = []
        for off in range(0, len(head), 16):
            chunk = head[off:off + 16]
            hexpart = " ".join(f"{b:02x}" for b in chunk)
            asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            rows.append(f"{off:08x}  {hexpart:<47}  {asciipart}")
        return rows

    @staticmethod
    def _detect_crlf(full: str) -> bool:
        """True if the existing file is DOMINANTLY Windows CRLF (sample the head). Used to PRESERVE
        line endings on edit: the model emits '\\n', and writing that to a CRLF file rewrites every line
        ending — a huge spurious diff / corruption on Windows-authored repos. DOMINANCE (not mere
        presence): a mostly-LF file with one embedded '\\r\\n' (a byte literal, an HTTP fixture, a merge
        artifact) must NOT be flipped whole-file to CRLF — while a uniformly-CRLF file with one stray LF
        still counts as CRLF. crlf ≥ (bare-LF) covers both, and keeps the pinned uniform cases."""
        try:
            with open(full, "rb") as f:
                head = f.read(65536)
        except OSError:
            return False
        crlf = head.count(b"\r\n")
        lf_only = head.count(b"\n") - crlf          # LFs that are NOT part of a CRLF
        return crlf > 0 and crlf >= lf_only

    @staticmethod
    def _preserve_eol(text: str, crlf: bool) -> str:
        """Convert `text` to CRLF iff the target file is CRLF (normalize first → idempotent, handles
        mixed input). No-op for the common LF case, so LF files never gain spurious '\\r'."""
        return text.replace("\r\n", "\n").replace("\n", "\r\n") if crlf else text

    def _t_list_files(self, args: dict) -> str:
        path = args.get("path") or "."
        hf = self._history_route(path)
        if hf is not None:               # list the virtual history/ namespace (index.md + turn-N.md)
            return hf.listing(self._archive_handle(path))
        base = self.resolve_read(path)
        if not args.get("recursive"):
            entries = sorted(os.listdir(base))
            shown = [e + "/" if os.path.isdir(os.path.join(base, e)) else e
                     for e in entries if not _is_ignored(e)]
            hidden = [e for e in entries if _is_ignored(e)]
            # Same bound as the recursive branch nine lines below (the review's G3: one uncapped
            # call injected ~27.5k tokens into a turn that reported 1/1 succeeded).
            capped = len(shown) > _LIST_CAP
            shown = shown[:_LIST_CAP]
            body = "\n".join(shown) or "(empty)"
            if capped:
                body += f"\n(+more — capped at {_LIST_CAP}; pass a subdirectory path to narrow)"
            if hidden:  # name them so the model KNOWS they exist (recoverable), without flooding
                body += f"\n(+{len(hidden)} ignored: {', '.join(hidden[:6])})"
            return body
        # recursive: a clean, ignore-pruned, bounded repo MAP — the native alternative to shell `find`
        rels: list[str] = []
        capped = False
        for dirpath, dirnames, filenames in os.walk(base):  # symlinks not followed (no .venv loops)
            dirnames[:] = sorted(d for d in dirnames if not _is_ignored(d))  # prune in place → don't descend
            rel = os.path.relpath(dirpath, base)
            for f in sorted(filenames):
                if _is_ignored(f):
                    continue
                rels.append(f if rel == "." else norm_rel(os.path.join(rel, f)))
                if len(rels) >= _LIST_CAP:
                    capped = True
                    break
            if capped:
                break
        body = "\n".join(sorted(rels)) or "(empty)"
        if capped:
            body += f"\n(+more — capped at {_LIST_CAP}; pass a subdirectory path to narrow)"
        return body

    def _t_edit_file(self, args: dict) -> str:
        rej = self._history_readonly_guard(args.get("path", ""))
        if rej is not None:
            return rej
        full = self.resolve_read(args["path"])   # I2: target the SAME file read_file shows (existing match across roots); new files still land at the focus base
        stale = self._stale_write_guard(args["path"], full)
        if stale is not None:
            return stale
        self._mkparent(full)
        content = args["content"]
        if os.path.exists(full):                      # preserve the file's existing line endings (CRLF)
            content = self._preserve_eol(content, self._detect_crlf(full))
        self._journal(args["path"], full)
        self._atomic_write(full, content)
        self._mark_read(full)
        if content[:2] == "#!":          # a shebang script should be runnable (general, task-agnostic)
            self._make_executable(full)
        msg = f"Wrote {len(content)} bytes to {args['path']}"
        try:                             # echo the head so the model sees what landed (post-EOL-normalization)
            n = content.replace("\r\n", "\n").rstrip("\n").count("\n") + 1 if content.strip() else 0
            return f"{msg} ({n} lines). Head:\n" + _numbered_window(content, 0, 15, ctx=0, cap=16)
        except Exception:  # noqa: BLE001 — the echo must never fail the write
            return msg

    def _make_executable(self, full: str) -> None:
        """chmod +x a freshly-written shebang script (a script the agent declared executable via '#!'
        should run without a separate chmod). Best-effort; never fails the write."""
        try:
            import stat as _stat
            os.chmod(full, os.stat(full).st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
        except OSError:
            pass

    def _t_append(self, args: dict) -> str:
        rej = self._history_readonly_guard(args.get("path", ""))
        if rej is not None:
            return rej
        full = self.resolve_read(args["path"])   # I2: append to the SAME file read_file shows; new files still land at the focus base
        self._mkparent(full)
        self._journal(args["path"], full)
        content = args["content"]
        if os.path.exists(full):
            content = self._preserve_eol(content, self._detect_crlf(full))
        with open(full, "ab") as f:   # byte-exact (like write_file's "wb") — text mode would translate newlines, corrupting CRLF
            f.write(content.encode("utf-8"))
        self._mark_read(full)   # the agent's OWN append must not poison the staleness mark
        msg = f"Appended {len(content.encode('utf-8'))} bytes to {args['path']}"
        try:                             # echo the file tail so the model sees the appended content in context
            with open(full, encoding="utf-8", errors="replace") as _f:
                whole = _f.read()
            total = whole.replace("\r\n", "\n").rstrip("\n").count("\n") + 1
            app = content.replace("\r\n", "\n").rstrip("\n").count("\n") + 1
            return f"{msg}. File tail:\n" + _numbered_window(whole, max(0, total - app), total - 1, ctx=2)
        except Exception:  # noqa: BLE001
            return msg

    def _edit_result(self, path: str, before: str, after: str, change_offset: int, new_text: str,
                     *, fuzzy: bool = False) -> str:
        """str_replace result: byte delta + a numbered POST-EDIT window around the change, so the model sees
        the file's CURRENT state in-transcript. Best-effort — falls back to the plain byte message."""
        tag = " (normalized/fuzzy match)" if fuzzy else ""
        msg = f"Replaced 1 occurrence{tag} in {path} ({len(before)} → {len(after)} bytes)"
        try:
            s0 = before[:change_offset].count("\n")             # 0-based start line (unchanged prefix ⇒ same in `after`)
            e0 = s0 + new_text.replace("\r\n", "\n").count("\n")
            return f"{msg}. Updated region (lines {s0 + 1}-{e0 + 1}):\n" + _numbered_window(after, s0, e0)
        except Exception:  # noqa: BLE001 — the echo must never fail the edit
            return msg

    def _t_str_replace(self, args: dict) -> str:
        rej = self._history_readonly_guard(args.get("path", ""))
        if rej is not None:
            return rej
        full = self.resolve_read(args["path"])   # I2: edit the SAME file read_file shows (search all roots), not a focus-relative phantom
        try:
            cur = self.read_text(full, lossy=False)  # read the resolved target; strict: abort on invalid UTF-8, never write back a mangled file
        except BinaryTextError as ex:
            return ToolText(
                f"{ex}. str_replace did not run; use a binary-aware command or replace the complete asset.",
                status=ToolStatus.STEERED,
            )
        except UnicodeDecodeError as ex:
            # actionable error (not an opaque codec traceback) — read_file shows the file as editable, so name
            # the cause + the fallback rather than half-disagreeing with the display path.
            return ToolText(
                f"{args['path']} contains a non-UTF-8 byte ({ex}); str_replace can't safely edit it "
                "(a whole-file write-back would corrupt the other bytes). Use edit_file to rewrite the file, "
                "or fix its encoding first.",
                status=ToolStatus.STEERED,
            )
        crlf = self._detect_crlf(full)                # preserve the file's line endings on write-back
        old = args["old_string"]
        new = args["new_string"]
        # OPEN FILES renders with cat -n line numbers; if the model pasted a numbered snippet back into
        # old_string, strip the "  N\t" prefixes so it still matches the real (unnumbered) file. Tried only
        # as a FALLBACK after the raw text, and only when EVERY line carried a number (clearly cat -n output,
        # not source) — so a real match is never altered.
        candidates = [old]
        stripped = _strip_line_numbers(old)
        if stripped != old:
            candidates.append(stripped)
        # PRIMARY: exact match (raw first, then de-numbered). >1 is ambiguous UNLESS replace_all is set.
        replace_all = bool(args.get("replace_all"))
        for cand in candidates:
            n = cur.count(cand)
            if n == 0:
                continue
            if n == 1 or replace_all:
                updated = self._preserve_eol(cur.replace(cand, new, n if replace_all else 1), crlf)
                self._journal(args["path"], full)
                self._atomic_write(full, updated)
                self._mark_read(full)   # the agent's OWN write must not poison the staleness mark
                return self._edit_result(args["path"], cur, updated, cur.index(cand), new)
            return ToolText(
                f"old_string occurs {n} times in {args['path']}; add context to make it unique, "
                "or pass replace_all=true to change them all",
                status=ToolStatus.STEERED,
            )
        # FALLBACK: whitespace-tolerant UNIQUE fuzzy span (raw first, then de-numbered). fuzzy_find_unique
        # returns None on 0/>1 candidates, so uniqueness is preserved — we never replace an ambiguous match.
        for cand in candidates:
            span = fuzzy_find_unique(cur, cand)
            if span is not None:
                updated = self._preserve_eol(cur[:span[0]] + new + cur[span[1]:], crlf)
                self._journal(args["path"], full)
                self._atomic_write(full, updated)
                self._mark_read(full)   # the agent's OWN write must not poison the staleness mark
                return self._edit_result(args["path"], cur, updated, span[0], new, fuzzy=True)
        return ToolText(
            f"old_string not found in {args['path']} — your snippet does not match the file. Copy the EXACT "
            "text from OPEN FILES (the live content, WITHOUT the line-number prefix), or rewrite the whole "
            "file with edit_file. Do NOT retry the same str_replace.",
            status=ToolStatus.STEERED,
        )

    # --- edit journal (powers /undo) -----------------------------------------
    def _mark_read(self, full: str) -> None:
        """Record (mtime_ns, size) when a file is read, so a later whole-file write can prove the
        file is still what the model saw. The generation-time read→write window is where a human
        save (or a branch switch) gets silently destroyed (the review's Family E — the only
        data-loss finding). Bounded: the mark map is cleared per-turn by the caller's lifecycle."""
        try:
            st = os.stat(full)
            self._read_marks[full] = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._read_marks.pop(full, None)

    def _stale_write_guard(self, rel: str, full: str):
        """Refuse a whole-file overwrite when the file changed on disk since the last read_file.

        claude-code's refuse model (Hermes' mtime-warning is advisory-only and too weak; a guard in
        the TOOL, not in model judgement). No mark → nothing proven stale → allowed (a write
        without a prior read is the caller's risk, matching today). The refusal is a STEERED
        redirect (↷, never ✗): re-read, then re-issue. /undo restores if a write already landed.
        """
        mark = self._read_marks.get(full)
        if mark is None or not os.path.exists(full):
            return None
        try:
            st = os.stat(full)
        except OSError:
            return None
        if (st.st_mtime_ns, st.st_size) == mark:
            return None
        return ToolText(
            f"refusing to overwrite {rel}: the file changed on disk since the last read_file "
            f"(was {mark[1]} bytes, now {st.st_size} bytes) — re-read it, then retry the edit "
            "(/undo restores the previous contents if a write already landed)",
            status=ToolStatus.STEERED,
        )

    def _journal(self, rel: str, full: str) -> None:
        """Record a file's pre-image (or None if it didn't exist) just before a write, so /undo can revert
        the most recent edit. Bounded ring — recent edits only, never an unbounded history."""
        try:
            if os.path.exists(full):
                with open(full, "rb") as _f:
                    prev = _f.read()
            else:
                prev = None
        except OSError:
            prev = None
        self._edit_journal.append((rel, full, prev))
        if len(self._edit_journal) > 50:
            del self._edit_journal[:-50]

    def undo_last(self) -> str:
        """Revert the most recent journaled edit. Returns a human-readable result for the UI."""
        if not self._edit_journal:
            return "Nothing to undo."
        rel, full, prev = self._edit_journal.pop()
        try:
            if prev is None:
                if os.path.exists(full):
                    os.remove(full)
                return f"Undid: removed {rel} (it did not exist before that edit)."
            with open(full, "wb") as f:
                f.write(prev)
            return f"Undid the last edit to {rel} ({len(prev)} bytes restored)."
        except OSError as e:
            return f"Undo failed for {rel}: {e}"

    def attach_image(self, path: str) -> str:
        """Stash a workspace image for the NEXT seed build as a vision content part. Returns a status line.
        Gated by the caller (only called for a vision-capable model). Confined to the workspace like reads.
        The MIME type is sniffed from MAGIC BYTES (not the extension), so a spoofed extension can't smuggle a
        non-image through as image/png."""
        import base64
        try:
            full = self.resolve_read(path)
            with open(full, "rb") as _f:
                raw = _f.read()
        except OSError as e:
            return f"Error: cannot read image {path}: {e}"
        if len(raw) > 8 * 1024 * 1024:
            return f"Error: image {path} is {len(raw)} bytes (cap 8MB) — too large to attach"
        mime = _sniff_image_mime(raw)
        if mime is None:
            return f"Error: {path} is not a recognized image (png/jpeg/gif/webp/bmp) — not attached"
        self.pending_images.append({"path": path, "b64": base64.b64encode(raw).decode("ascii"), "mime": mime})
        # cost-awareness: a base64 image is large + billed as image tokens → this turn costs more than text.
        return f"attached image {path} ({len(raw) // 1024} KB, {mime}) — vision turn, costs more than a text turn"

    def _t_code_review(self, args: dict) -> str:
        """Return a diff plus an explicit tracked/untracked/ignored inventory."""
        import subprocess
        ref = (args.get("ref") or "HEAD").strip() or "HEAD"
        include_ignored = bool(args.get("include_ignored"))
        # SECURITY: `ref` is model-controlled. An option-shaped ref (e.g. --output=/path, -O, --ext-diff)
        # would be parsed by git as a FLAG → arbitrary out-of-workspace file write / command exec, bypassing
        # the file-tool confinement. Reject leading-dash refs (a real ref/range never starts with '-') and
        # pass `--` so the ref can never be read as an option. Valid ranges (main...HEAD, HEAD~3) still work.
        if ref.startswith("-"):
            return ToolText(f"Error: invalid ref {ref!r} (a ref must not start with '-').", ok=False)
        base = [
            "git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
            "-C", self.root(),
        ]
        try:
            # `git diff` omits untracked files. An uncertainty observation that says "No changes" on a workspace
            # containing them is false evidence, so always pair it with a porcelain inventory. Disable a
            # repo-configured fsmonitor command: merely observing an untrusted repo must not execute it.
            status = subprocess.run(
                [*base, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=30,
            )
            ignored = None
            if include_ignored:
                # `git status` deliberately hides ignored paths, but an uncertain command can write them too.
                # Enumerate recursively only for the expensive uncertainty view; ordinary reviews stay lean.
                ignored = subprocess.run(
                    [*base, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=30,
                )
            # --no-ext-diff / --no-textconv: a hostile repo's .gitattributes + .git/config can register a diff
            # driver whose external/textconv command git would otherwise EXECUTE while rendering the diff
            # (external review H-06). Disable both so reviewing a repo never runs repo-controlled helpers.
            p = subprocess.run(
                [*base, "diff", "--no-ext-diff", "--no-textconv", ref, "--"],
                stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            return ToolText("Error: git is not installed.", ok=False)
        except subprocess.SubprocessError as e:
            return ToolText(f"Error: git workspace observation failed ({type(e).__name__}: {e}).", ok=False)
        if status.returncode != 0:
            return ToolText(f"Error: `git status` failed — {status.stderr.strip()[:300]} "
                            "(is this a git repo?)", ok=False)
        if ignored is not None and ignored.returncode != 0:
            return ToolText(f"Error: ignored-file inventory failed — {ignored.stderr.strip()[:300]} "
                            "(workspace observation is incomplete)", ok=False)
        if p.returncode != 0:
            return ToolText(f"Error: `git diff {ref}` failed — {p.stderr.strip()[:300]} "
                            "(is this a git repo? is the ref valid?)", ok=False)
        diff = p.stdout
        # NUL-delimited porcelain makes hostile newline/control filenames unambiguous. repr() keeps those
        # delimiters escaped in the model-visible inventory instead of letting a filename forge a status row.
        rows = [repr(row) for row in status.stdout.split("\0") if row]
        ignored_paths = [
            row for row in (ignored.stdout.split("\0") if ignored is not None else ()) if row
            # Do not let code_review's own paged blobs make each subsequent review invent another blob.
            and not row.replace("\\", "/").startswith(".sliceagent/blobs/workspace-review-")
        ]
        ignored_rows = [repr("!! " + row) for row in ignored_paths]
        if len(ignored_rows) > 240:
            import hashlib
            digest = hashlib.sha256("\0".join(ignored_paths).encode("utf-8", "surrogatepass")).hexdigest()
            omitted = len(ignored_rows) - 240
            ignored_rows = [
                *ignored_rows[:200],
                f"'!! … {omitted} additional ignored paths represented by manifest sha256:{digest}'",
                *ignored_rows[-40:],
            ]
        inventory_rows = [*rows, *ignored_rows]
        tracked_inventory = "\n".join(rows) if rows else "(no tracked or untracked changes)"
        ignored_inventory = ("\n".join(ignored_rows) if ignored_rows else
                             "(no ignored files)" if include_ignored else "(not enumerated)")
        inventory = ("\n".join(inventory_rows) if inventory_rows else
                     "(clean: no tracked or untracked changes; ignored files not enumerated)"
                     if not include_ignored else
                     "(clean: no tracked, untracked, or ignored files outside HEAD)")
        marker = ("[workspace observation: tracked + untracked + ignored inventory complete]"
                  if include_ignored else "[code review: tracked + untracked inventory]")
        if not diff.strip() and not inventory_rows:
            suffix = (" and no untracked or ignored files exist" if include_ignored else
                      " and no untracked files exist (ignored files were not enumerated)")
            body = (f"{marker}\nGit status:\n{inventory}\n\nNo changes vs {ref} — the tracked "
                    f"working tree matches it{suffix}. Nothing to review.")
        elif not diff.strip():
            body = (f"{marker}\nGit status (includes files omitted by git diff):\n{inventory}\n\n"
                    f"No tracked diff vs {ref}; inspect the listed untracked/ignored/status entries before concluding "
                    "the workspace is unchanged.")
        else:
            # Put the actionable tracked diff before the potentially large ignored manifest so ordinary code
            # review remains useful; the full computed observation is still retained/paged as one value.
            body = (f"{marker}\nGit status (tracked + untracked):\n{tracked_inventory}\n\n"
                    f"git diff {ref} ({len(diff)} chars). Review for correctness, security, and edge cases; "
                    f"cite file:line per issue.\n\n{diff}\n\n"
                    f"Ignored-file inventory ({'complete computation' if include_ignored else 'not requested'}; "
                    f"bounded presentation):\n{ignored_inventory}")
        # A large observation is paged losslessly after both commands completed; the full detail remains
        # available for analysis while the typed observation can still prove that live inventory ran.
        return self._page_out(body, label=f"workspace-review-{ref}")

    def _t_ask_user(self, args: dict) -> str:
        q = (args.get("question") or "").strip()
        if not q:
            return ToolText("Error: ask_user requires a non-empty 'question'.", ok=False)
        opts = args.get("options")
        opts = [str(o) for o in opts] if isinstance(opts, list) and opts else None
        try:
            ans = (self.on_ask_user or _default_ask_user)(q, opts)
        except (EOFError, KeyboardInterrupt):
            ans = "(no answer)"
        answer = str(ans).strip()
        if not answer or answer.casefold() in {"(no answer)", "(cancelled)", "(canceled)"}:
            return ToolText("No user answer was received.", status=ToolStatus.CANCELLED)
        return f"User answered: {answer}"

    def _call_timeout(self, raw) -> float:
        """Per-call blocking deadline: `raw` seconds, default self.timeout, hard ceiling 600s. Shared by
        every blocking runner so a slow build never dies at the 30s default in one tool but not another."""
        try:
            t = float(raw or self.timeout)
        except (TypeError, ValueError):
            t = float(self.timeout)
        return max(1.0, min(t, 600.0))

    def _proc_tools_available(self) -> bool:
        """True when the proc_* family is actually registered for the model. The default build
        deregisters all of them (cli.py, unless AGENT_ADVANCED_TOOLS) — remediation text and the
        adoption path must never name tools the model cannot call."""
        return self.registry.has("proc_start") and self.registry.has("proc_wait")

    @staticmethod
    def _timeout_shape(command: str) -> str:
        """Canonical identity of a timed-out command for hang detection: strip leading env
        assignments and one `cd X &&` hop, then keep the first two tokens (`npx eslint`,
        `npm test`, …). File-argument variants of the same tool must share a shape — laddering
        `eslint .` into per-file `eslint src/App.tsx` is the SAME hang, not a new experiment."""
        text = str(command or "").strip()
        text = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*", "", text)
        text = re.sub(r"^cd\s+\S+\s*&&\s*", "", text)
        text = re.sub(r"^\(+", "", text)
        return " ".join(text.split()[:2])[:80]

    def _timeout_escalation(self, t: float, ceiling_hit: bool, *, proc_tools: bool | None = None,
                            command: str = "") -> str:
        """The remediation the model needs AT the failure. Without it a timeout reads as a dead end and the
        next step is a blind retry at the same limit — the deadline is a TOOL CHOICE, not a verdict.
        Composed from the tools the CALLER can actually use: naming proc_start to a build — or a
        scoped child — that cannot call it would send the agent to a tool it does not have
        (Family J — advice to nowhere; the review's U1 at the message level).

        REPEAT-AWARE (the loom-app eslint spiral, 2026-08-03): the first-timeout text says
        're-run with a larger timeout' — and a model will OBEY it up the whole ladder
        (240→600→120→90) against a genuinely hung target, burning the turn. The second timeout
        of the SAME command shape this session flips the advisory to hang mode: stop laddering,
        record the check as unrunnable, move on."""
        shape = self._timeout_shape(command) if command else ""
        seen = getattr(self, "_timeout_shapes", None)
        if seen is None:
            seen = self._timeout_shapes = {}
        repeats = seen.get(shape, 0)
        if shape:
            seen[shape] = repeats + 1
            if len(seen) > 64:   # bounded like every host-side tally
                seen.pop(next(iter(seen)))
        if shape and repeats >= 1:
            return (
                f"Exit code 124 — the {t:g}s deadline again: `{shape}` has now timed out "
                f"{repeats + 1}x this session. Treat it as a HANG, not a slow command.\n"
                "Next: do NOT re-run this with a longer timeout or per-file splits — record the "
                "check as unrunnable (a finding: name the command and the hang), and continue "
                "with the remaining verification. Notes: this tool's timeout argument IS the "
                "watchdog (macOS has no `timeout`(1)); and never pipe a gate "
                "(`… | tail; echo $?` reports the PIPE's exit, masking the real one — run gates bare)."
            )
        if proc_tools is None:
            proc_tools = self._proc_tools_available()
        if proc_tools:
            next_step = ("this is the 600s ceiling — re-run it under proc_start, then proc_wait/proc_tail "
                         "(background processes are not bounded by this deadline)"
                         if ceiling_hit else
                         "re-run with a larger timeout (up to 600), or for genuinely long work use "
                         "proc_start + proc_wait/proc_tail, which this deadline does not bound")
        else:
            next_step = ("this is the 600s ceiling — for genuinely long work, split the command or "
                         "run its stages separately so each fits the ceiling"
                         if ceiling_hit else
                         "re-run with a larger timeout (up to 600), or split the command so each "
                         "stage fits the deadline")
        return ("Exit code 124 — the {t:g}s deadline, not the command's own status. The process group was "
                "reaped, so whatever it had already written is still on disk.\nNext: {escalate}").format(
            t=t, escalate=next_step,
        )

    def _adopt_on_timeout(self, command: str, timeout_s: float):
        """The sandbox on_timeout hook: adopt the LIVE process into the background registry instead
        of reaping it (Kimi Code's autoBackgroundOnTimeout — a deadline detaches, it does not kill).
        Only offered when the proc_* family is registered: an adopted process must be followable and
        stoppable by the model, or a timed-out command stays alive by design with nothing able to
        see or stop it. Adoption failure returns None, which falls back to the ordinary
        bounded-failure reap."""
        def adopt(process, log_path, log_fh):
            try:
                handle = self.procs.adopt(process, command, self.root(), log_path, log_fh)
            except Exception:  # noqa: BLE001 — never let adoption itself fail the command
                return None
            follow = []
            if self.registry.has("proc_tail"):
                follow.append(f"follow with proc_tail {handle}")
            if self.registry.has("proc_wait"):
                follow.append(f"wait with proc_wait {handle}")
            if self.registry.has("proc_kill"):
                follow.append(f"stop with proc_kill {handle}")
            tail = ("; ".join(follow) if follow else
                    "it is tracked by the host; ask the user to stop it if needed")
            return (f"Timed out after {timeout_s:g}s — but the command was NOT killed. It now runs "
                    f"in the background as {handle}: its work so far is preserved in its log and "
                    f"new output keeps landing there; {tail}. Side effects it already "
                    f"produced remain on disk; more may still land while it runs.")
        return adopt

    def _t_run_command(self, args: dict) -> str:
        # Optional per-call timeout (default self.timeout, hard ceiling 600s) so slow builds don't
        # die at the 30s default and come back as exit 124. Long-lived processes use proc_start.
        t = self._call_timeout(args.get("timeout"))
        # A scoped caller denied the proc_* family marks its call NO_ADOPT (ScopedSurface): the
        # adoption gate must reflect the surface the CALLER can use, not the host registry — an
        # adopted process whose follow tools the caller cannot call stays alive, followable by
        # no one, while the outcome types SUCCEEDED (the review's U1).
        no_adopt = bool(args.pop(NO_ADOPT_ON_TIMEOUT_ARG, False))
        activity_cb = None
        if callable(self._verify_notify):
            # Liveness as EVIDENCE (the review's Family H): the status line shows the output byte
            # count growing ~1/s while the command runs — a frozen counter names a stall, a growing
            # one names progress. Rides the presentation-only host_activity channel, never the journal.
            label = " ".join(str(args.get("command") or "").split())[:40]
            def activity_cb(nbytes):
                try:
                    kb = nbytes / 1024
                    self._verify_notify(f"run · {label} · {kb:.1f} KB output")
                except Exception:  # noqa: BLE001 — liveness must never affect the command
                    pass
        # Bind liveness on THIS thread (cancel_scope), not on the shared sandbox attribute: a
        # concurrent turn's run_command used to overwrite/restore the one slot and steal or leak
        # the callback — the same shared-slot shape as the cancel token (criticals 1&2).
        prev_cb = cancel_scope.bind_activity(activity_cb) if activity_cb is not None else None
        try:
            code, out = self.sandbox.run(
                args["command"], cwd=self.root(), timeout=t,
                on_timeout=(self._adopt_on_timeout(args["command"], t)
                            if self._proc_tools_available() and not no_adopt else None),
            )
        finally:
            if activity_cb is not None:
                cancel_scope.unbind_activity(prev_cb)
        self._grant_shell_paths(args.get("command", ""))  # I2 reach=action: dirs the shell touched
        out = out.strip()
        if code == SANDBOX_ADOPTED:
            # The deadline passed but NOTHING was killed: the live process joined the background
            # registry with its progress intact. Not ✗ and not a verdict — the handle and the
            # follow-up tools are the result.
            return ToolText(out)
        if code == SANDBOX_TIMEOUT:
            # A deadline reap is a DELIBERATE, bounded stop with a known cause — not an unknown
            # effect. Typing it indeterminate parked the whole turn and made the escalation below
            # unreachable; FAILED lets the model re-run with a larger timeout or proc_start, exactly
            # as the message says. The partial-write warning stays because it is true.
            return ToolText(
                f"{self._timeout_escalation(t, t >= 600.0, proc_tools=(self._proc_tools_available() and not no_adopt), command=args.get('command', ''))}\n"
                f"{self._page_out(out, label='command output') or '(no output)'}",
                ok=False,
            )
        if code != 0:
            return ToolText(f"Exit code {code}\n{self._page_out(out, label='command output') or '(no output)'}", ok=False)
        return self._page_out(out, label="command output") if out else "(command produced no output)"

    # --- background / long-running processes (procman) ---
    def _host_only_note(self) -> str:
        # #4: background procs + PTY sessions run on the HOST, not through self.sandbox. Under a non-local
        # sandbox (e.g. docker) that defeats container isolation — surface it instead of silently bypassing.
        return ("[warning: this runs on the HOST, NOT inside the configured sandbox — "
                f"{type(self.sandbox).__name__} isolation does not apply]\n"
                if type(self.sandbox).__name__ != "LocalSandbox" else "")

    def _t_proc_start(self, args: dict) -> str:
        h = self.procs.start(args["command"], cwd=self.root())
        return (f"{self._host_only_note()}Started background process {h}: {args['command']}\n"
                f"Use proc_tail/proc_poll/proc_wait/proc_kill with handle {h}.")

    def _t_proc_poll(self, args: dict) -> str:
        return self.procs.poll(args["handle"])

    def _t_proc_tail(self, args: dict) -> str:
        # #26: cap requested lines so a huge `lines` can't dump a chatty server's whole log into the slice.
        try:
            n = int(args.get("lines") or 40)
        except (TypeError, ValueError):
            n = 40   # a non-numeric `lines` arg must not crash the tool
        return self.procs.tail(args["handle"], max(1, min(n, 2000)))

    def _t_proc_wait(self, args: dict) -> str:
        try:
            t = float(args.get("timeout") or 30.0)
        except (TypeError, ValueError):
            t = 30.0
        # proc_wait is a poll-with-timeout — allow sub-second waits (unlike run_command's 1s floor).
        # Liveness as EVIDENCE (U7 reaches this sibling too): the status line shows the watched
        # process's log growing ~1/s, so a frozen counter names a stall instead of looking like
        # a crash. Same thread-scoped binding as run_command.
        activity_cb = None
        if callable(self._verify_notify):
            handle_label = str(args.get("handle") or "?")
            def activity_cb(nbytes):
                try:
                    kb = nbytes / 1024
                    self._verify_notify(f"proc_wait · {handle_label} · {kb:.1f} KB output")
                except Exception:  # noqa: BLE001 — liveness must never affect the wait
                    pass
        prev_cb = cancel_scope.bind_activity(activity_cb) if activity_cb is not None else None
        try:
            return self.procs.wait(args["handle"], max(0.05, min(t, 600.0)))
        finally:
            if activity_cb is not None:
                cancel_scope.unbind_activity(prev_cb)

    def _t_proc_kill(self, args: dict) -> str:
        try:
            return self.procs.kill(args["handle"])
        except ProcessGroupTerminationError as exc:
            return ToolText(f"Error: INDETERMINATE process teardown: {exc}", status="indeterminate")

    # --- interactive PTY sessions (terminal) ---
    def _t_terminal_open(self, args: dict) -> str:
        name = args.get("session") or "main"
        problem = self.terminals.open_problem(name)
        if problem:
            return ToolText(problem, status=ToolStatus.STEERED)
        self.terminals.open(name, cwd=self.root(), command=args.get("command") or None)
        banner = self.terminals.peek(name, timeout=0.6)  # peek, not read — don't eat the first prompt
        return f"{self._host_only_note()}Opened terminal session {name!r}.\n{banner}"

    def _t_terminal_send(self, args: dict) -> str:
        name = args.get("session") or "main"
        enter = args.get("enter")
        enter = True if enter is None else bool(enter)
        return self.terminals.send(name, args["input"], enter=enter)

    def _t_terminal_read(self, args: dict) -> str:
        name = args.get("session") or "main"
        try:
            t = float(args.get("timeout") or 1.0)
        except (TypeError, ValueError):
            t = 1.0
        return self._page_out(self.terminals.read(name, timeout=max(0.05, min(t, 120.0))), label="terminal output")

    def _t_terminal_wait(self, args: dict) -> str:
        name = args.get("session") or "main"
        try:
            t = float(args.get("timeout") or 10.0)
        except (TypeError, ValueError):
            t = 10.0
        return self.terminals.wait(name, args["until"], timeout=max(0.1, min(t, 600.0)))

    def _t_terminal_close(self, args: dict) -> str:
        try:
            return self.terminals.close(args.get("session") or "main")
        except ProcessGroupTerminationError as exc:
            return ToolText(f"Error: INDETERMINATE terminal teardown: {exc}", status="indeterminate")

    # --- world model (durable agent scratchpad; state lives in the Slice, folded by slice_sink) ---
    def _t_world_set(self, args: dict) -> str:
        k = (args.get("key") or "").strip()
        if not k:
            return ToolText("Error: world_set requires a non-empty 'key'.", ok=False)
        v = " ".join(str(args.get("value", "")).split())   # one-line echo so the value is readable THIS turn
        if len(v) > 200:
            v = v[:200] + "…"
        return (f"WORLD MODEL: saved {k!r} = {v} (in your WORLD MODEL section from your NEXT turn; "
                f"this turn, re-read it from this call).")

    def _t_world_clear(self, args: dict) -> str:
        k = (args.get("key") or "").strip()
        return f"WORLD MODEL: cleared {repr(k) if k else '(all keys)'}."

    def _t_reconcile_execution(self, args: dict) -> str:
        resolution = " ".join(str(args.get("resolution") or "").split())
        if not resolution:
            return ToolText("Error: reconcile_execution requires an observed resolution.", ok=False)
        return f"INDETERMINATE EXECUTION reconciled from live observation: {resolution}"

    # --- standing requirements (the durable contract; state lives in the Slice, folded by slice_sink) ---
    def _t_require(self, args: dict) -> str:
        t = " ".join((args.get("text") or "").split())
        if not t:
            return ToolText("Error: require needs a non-empty 'text'.", ok=False)
        return f"REQUIREMENT recorded: {t} (in your STANDING REQUIREMENTS from your next turn until done/dropped)."

    def _t_requirement_done(self, args: dict) -> str:
        t = " ".join((args.get("text") or "").split())
        if not t:
            return ToolText("Error: requirement_done needs the requirement 'text'.", ok=False)
        return f"REQUIREMENT marked done: {t} (stays shown as [x], no longer flagged outstanding)."

    def _t_drop_requirement(self, args: dict) -> str:
        t = " ".join((args.get("text") or "").split())
        if not t:
            return ToolText("Error: drop_requirement needs the requirement 'text'.", ok=False)
        return f"REQUIREMENT dropped: {t}."

    def _t_supersede_requirement(self, args: dict) -> str:
        old = " ".join((args.get("old_text") or "").split())
        new = " ".join((args.get("new_text") or "").split())
        if not old or not new:
            return ToolText("Error: supersede_requirement needs non-empty old_text and new_text.", ok=False)
        return f"REQUIREMENT supersession requested: {old} → {new}."

    def _run_verify_command(self, command: str):
        # Announce the live command so a long verify (a real pytest run) is visible on the status
        # line instead of a generic `update_work` — presentation only, never a gate.
        notify = self._verify_notify
        if callable(notify):
            try:
                notify(f"verify · {command}")
            except Exception:  # noqa: BLE001
                pass
        runner = self._verify_runner
        if callable(runner):
            return runner(command)
        from sliceagent_core.execution import ToolStatus as _TS
        from .oracle import CommandOracle, OracleResult
        # A command whose program is not on PATH answers 127, which is indistinguishable from a real red
        # check — the host would tell the model to fix perfectly good work because `pytest` was never
        # installed. Resolve it first and report the same NO-VERDICT class a deadline overrun reports.
        missing = _unrunnable_verify_program(command)
        if missing:
            return OracleResult(_TS.INDETERMINATE, f"{missing!r} is not on PATH; the check never ran")
        # Return the typed OracleResult, not a flattened (ok, output): it still unpacks as that pair, but
        # it carries INDETERMINATE, which a bool cannot.
        return CommandOracle(command, root=self.root()).verify()

    def _t_update_work(self, args: dict) -> str:
        self._item_verify_green = {}
        try:
            graph, logical_id, workspace_epoch = self._active_work_snapshot()
            delta = build_work_delta(
                graph, args, logical_id=logical_id, workspace_epoch=workspace_epoch,
            )
            # Host acceptance gate (P2): an item landing on 'ready' with a verify contract must prove it
            # NOW — the host runs the commands once, here; green promotes ready->verified via the memo the
            # effect factory replays; red rejects the atomic batch loudly with the failing output.
            # Verification gates the TRANSITION INTO 'ready', never the resulting state. Keying on
            # state re-ran a proven item's commands whenever an unrelated field was touched, and left
            # a hole with teeth: a change that only INHERITS 'ready' (no status field) passed the
            # planning surface's status gate and then executed real shell from inside a read-only
            # planning turn. Gate and host now agree on the same trigger, so that class is closed.
            def _enters_ready(item) -> bool:
                previous = graph.get(item.id)
                return previous is None or previous.status != "ready"

            candidates = [(item.id, item.verify) for item in (*delta.creates, *delta.updates)
                          if item.status == "ready" and item.verify and _enters_ready(item)]
            if candidates:
                green, failure = run_item_verification(
                    candidates, self._run_verify_command, self._verify_attempts,
                )
                if failure:
                    return ToolText(f"Error: ACTIVE WORK update rejected: {failure}", ok=False)
                self._item_verify_green = {item_id: True for item_id in green}
                delta = build_work_delta(
                    graph, args, logical_id=logical_id, workspace_epoch=workspace_epoch,
                    verified_ok=frozenset(self._item_verify_green),
                )
            # Validate the full proposed graph now; effect construction repeats this against the same snapshot
            # and the reducer performs the one authoritative apply.
            proposed = graph.apply_delta(delta)
        except (ActiveWorkError, TypeError, ValueError) as exc:
            return ToolText(f"Error: ACTIVE WORK update rejected: {exc}", ok=False)
        roots = tuple(
            root for root in proposed.unresolved_roots
            if not logical_id or root.logical_id == logical_id
        )
        frontier = []
        if roots:
            root = roots[-1]
            frontier = [
                item for item in proposed.items
                if item.id != root.id and item.root_id == root.id
                and item.status in {"open", "in_progress", "waiting_user", "waiting_peer"}
            ]
        result = (
            f"ACTIVE WORK update accepted: {len(delta.creates)} created, "
            f"{len(delta.updates)} updated (base revision {delta.expected_revision})."
        )
        if self._item_verify_green:
            result += ("\nHost-verified (checks ran green, promoted ready->verified): "
                       + ", ".join(sorted(self._item_verify_green)))
        if frontier:
            shown = frontier[:12]
            result += "\nUnfinished current-request frontier: " + "; ".join(
                f"{item.id} [{item.status}]" for item in shown
            )
            if len(frontier) > len(shown):
                result += f"; +{len(frontier) - len(shown)} more"
            result += ". A settled batch does not retire these items."
        else:
            result += "\nUnfinished current-request frontier: none."
        return result

    def _t_execute_code(self, args: dict) -> str:
        out = self._execute_code(args["code"], timeout=self._call_timeout(args.get("timeout")))
        self._grant_shell_paths(args.get("code", ""))  # I2 reach=action: dirs code-as-action touched
        return out

    def _execute_code(self, code: str, *, timeout: float | None = None) -> str:
        """Code-as-action: run the model's script (prelude + code) in the sandbox, cwd=workspace.
        Only stdout returns. The script is written INSIDE the workspace as a hidden temp file
        (so it's mounted/available in every backend) and deleted right after; cwd is on sys.path
        so workspace imports resolve. `sandbox.python_cmd` keeps it backend-portable."""
        script = _CODE_PRELUDE + "\n# --- agent code ---\n" + code
        root = self.root()
        fd, path = tempfile.mkstemp(suffix=".py", prefix=".sliceagent-exec-", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(script)
            cmd = f"{shlex.quote(self.sandbox.python_cmd)} {shlex.quote(os.path.basename(path))}"
            t = self._call_timeout(timeout)
            code_n, out = self.sandbox.run(cmd, cwd=root, timeout=t)
            out = out.strip()
            # 124 is reserved by the in-script run() helper after it reaps a timed-out process group.
            # An outer sandbox timeout is the same shape: a DELIBERATE, bounded stop with a known
            # cause. FAILED (not indeterminate) keeps the turn alive so the model can re-run with a
            # raised deadline or proc_start, as the escalation says — an indeterminate typing used
            # to park the turn here and strand that advice.
            if code_n in (SANDBOX_TIMEOUT, 124):
                return ToolText(
                    # A script is a BATCH of edits: the deadline can land between them, so say so — the
                    # partial-write warning is the difference between re-running and re-running blind.
                    f"{self._timeout_escalation(t, t >= 600.0, command='execute_code script')}\n"
                    "Edits this script had already applied are on disk; re-read before re-running it.\n"
                    f"{self._page_out(out, label='execute_code output') or '(no output)'}",
                    ok=False,
                )
            if code_n != 0:
                return ToolText(f"Exit code {code_n}\n{self._page_out(out, label='execute_code output') or '(no output)'}", ok=False)
            return self._page_out(out, label="execute_code output") if out else "(execute_code produced no output)"
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    @staticmethod
    def _mkparent(path: str) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)

    @staticmethod
    def _atomic_write(full: str, content: str) -> None:
        """Write `content` to `full` atomically: write a temp file in the SAME directory,
        then os.replace() it over the target. A crash/error mid-write leaves the original
        intact (the rename is atomic on POSIX); the temp is unlinked on any failure. The
        temp must share the target's filesystem for os.replace to be atomic, hence
        dir=os.path.dirname(full) (full is already _resolve()'d)."""
        import stat as _stat
        d = os.path.dirname(full)
        # preserve the target's permission bits across the replace — else a str_replace/edit_file on an
        # existing 0755 script silently resets it to the mkstemp 0600 (drops the executable + group/other bits).
        # ONE stat in a try (no exists()+stat() TOCTOU): if the file is absent or concurrently removed, write
        # fresh with default perms rather than raising an unhandled FileNotFoundError.
        try:
            mode = _stat.S_IMODE(os.stat(full).st_mode)
        except OSError:
            mode = None
        fd, tmp = tempfile.mkstemp(prefix=".sliceagent-tmp-", dir=d)
        try:
            # newline="" disables the platform newline translation: _preserve_eol already normalized the
            # content's line endings (LF or CRLF) to match the target, so text-mode translation on Windows
            # would double-convert \n→\r\n inside an already-CRLF string (\r\r\n) and corrupt the file.
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            if mode is not None:
                os.chmod(tmp, mode)
            os.replace(tmp, full)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# Preserve the historical public name while new construction uses CodingToolHost.
LocalToolHost = CodingToolHost
