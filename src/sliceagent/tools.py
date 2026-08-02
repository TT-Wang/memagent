"""LocalToolHost — the default ToolHost.

Safe execution lives here: file ops resolve through a ReachSet containing the
primary project plus narrow grounded focus roots (with traversal and sensitive
blanket grants rejected), and shell runs through a Sandbox backend (sandbox.py).
The runtime's narrow catastrophic-command safeguard is composed at the loop boundary.

Note: Python's str.replace is literal, so str_replace has no $-pattern footgun
(unlike JS).
"""
from __future__ import annotations

import os
import posixpath
import re
import shlex
import shutil
import stat as _stat
import tempfile
from dataclasses import replace

from . import cancel_scope
from .active_work import (
    ActiveWorkError,
    EvidenceRef as WorkEvidenceRef,
    OutputRef as WorkOutputRef,
    ResourceRef as WorkResourceRef,
    UNRESOLVED_STATUSES,
    WorkDelta,
    WorkGraph,
    WorkItem,
)
from .access import AllAccess, FileAccess
from .binsniff import looks_binary
from .context import ResourceKind, ResourceRef, reserved_resource_ref
from .contextfs import ContextFS, is_context_path
from .execution import NO_ADOPT_ON_TIMEOUT_ARG, ToolEffect, ToolStatus
from .fuzzy import fuzzy_find_unique
from .platform_compat import (IS_WINDOWS, ProcessGroupTerminationError, is_win_abs,
                              msys_to_win, norm_rel, win_path_candidates)
from .procman import ProcManager
from .reach import ReachSet, ReachSteer, SENSITIVE_DIR_NAMES
from .registry import ToolEntry, ToolRegistry, ToolText
from .sandbox import SANDBOX_ADOPTED, SANDBOX_TIMEOUT, LocalSandbox
from .sensory_cortex import _is_ignored
from .terminal import SessionManager
from .workspace_handoff import WorkspaceScheduleDecision

# I1 PROVENANCE — host SELF-INFLICTED error sentinels. These name failures caused by the HOST's own
# capability boundaries (file-tool confinement or OS denial), NOT by a real bug in the user's code. Lesson
# mining filters pitfalls whose signature contains one of these so a turn whose only error was the
# agent hitting its OWN sandbox mines nothing (D2). Lower-cased substrings, matched task-agnostically;
# defined HERE (the source of these strings) so the denylist tracks the actual error messages.
HOST_ERROR_SENTINELS = (
    "path escapes the boundary",
    "file tools are confined",
    "permission denied",
    "operation not permitted",
)

# Prepended to every execute_code script: the in-sandbox tool helpers (code-as-action).
# No imports needed by the model. The workspace is cwd and on sys.path,
# Strip a leading "cat -n" line-number prefix ("   123\t") from a str_replace snippet pasted back from the
# numbered OPEN FILES render. Only fires when EVERY non-blank line has one (clearly cat -n output, not real
# source), so a genuine match is never altered; used as a fallback in _t_str_replace.
_LINENO_PREFIX = re.compile(r"^[ \t]*\d+\t")

def _strip_line_numbers(text: str) -> str:
    lines = text.split("\n")
    nonblank = [ln for ln in lines if ln.strip()]
    if not nonblank or not all(_LINENO_PREFIX.match(ln) for ln in nonblank):
        return text
    return "\n".join(_LINENO_PREFIX.sub("", ln) if ln.strip() else ln for ln in lines)

def _number_lines(lines, start: int = 1) -> str:
    """cat -n number a LIST of lines from `start` (1-based) — ABSOLUTE numbers so a windowed read still
    gives correct file:line evidence."""
    return "\n".join(f"{i:>6}\t{ln}" for i, ln in enumerate(lines, start))

def _numbered(text: str) -> str:
    """cat -n line numbers for read_file's RETURN, so the model gets file:line evidence IMMEDIATELY in-turn
    (same format as the OPEN FILES render). The number is a display prefix, NOT file content — str_replace
    strips a pasted prefix via _strip_line_numbers, so editing from a numbered read still matches."""
    return _number_lines(text.splitlines(), 1)

_READ_MAX_LINES = 1500   # default in-slice VIEW cap for read_file; the full file ALWAYS stays on disk (bound the view, not the file)
_READ_SLURP_CAP = 8 * 1024 * 1024     # bytes: above this, read_file streams the window instead of materializing the whole file
_READ_STREAM_CHUNK = 1024 * 1024      # streaming block size for huge-file window reads

def _coerce_int(v):
    """Tolerant int() for model-supplied args (str/float/None) — never raises."""
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError, OverflowError):
        return None

# so `import <workspace_module>` works for testing freshly-written code.
_CODE_PRELUDE = '''\
import os as _os, sys as _sys, subprocess as _sp
_sys.path.insert(0, _os.getcwd())

def _confine(path):
    # Confine code-as-action file helpers to the workspace (cwd = workspace root in the sandbox). Without
    # this, an absolute path or ../ escape let execute_code read/write outside allowed_roots, bypassing the
    # file-tool boundary. Shell (run_command) stays unconfined by design; these in-code helpers do not.
    _p = _os.path.realpath(path)
    _root = _os.path.realpath(_os.getcwd())
    if _p != _root and not _p.startswith(_root + _os.sep):
        raise PermissionError(f"path escapes the boundary: {path} (use run_command for paths outside it)")
    return path

def read_file(path):
    with open(_confine(path), encoding="utf-8") as _f: return _f.read()

def write_file(path, content):
    path = _confine(path)
    _d = _os.path.dirname(path)
    if _d: _os.makedirs(_d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as _f: _f.write(content)
    if content[:2] == "#!":  # a shebang script should be runnable (parity with the edit_file tool)
        try: _os.chmod(path, _os.stat(path).st_mode | 0o111)
        except OSError: pass
    return f"wrote {len(content)} bytes to {path}"

def append_file(path, content):
    path = _confine(path)
    _d = _os.path.dirname(path)
    if _d: _os.makedirs(_d, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as _f: _f.write(content)
    return f"appended {len(content)} bytes to {path}"

def str_replace(path, old, new):
    path = _confine(path)
    with open(path, encoding="utf-8", newline="") as _f: _cur = _f.read()
    _n = _cur.count(old)
    # RAISE, never return. A returned rejection reaches nobody unless the script happens to print it,
    # so a batch whose edit silently missed came back ok=True/SUCCEEDED — the model marked the item
    # ready and got a red verify on code it never changed. Raising aborts the script at the failed
    # edit and surfaces the reason as a real failure, which is what every other helper here does.
    if _n != 1: raise ValueError(
        f"str_replace: old_string occurs {_n}x in {path} (need exactly 1) — "
        f"add surrounding lines to make it unique, or write_file the whole file")
    with open(path, "w", encoding="utf-8", newline="") as _f: _f.write(_cur.replace(old, new, 1))
    return f"replaced 1 occurrence in {path}"

def list_files(path="."):
    return sorted(_os.listdir(_confine(path)))

def _run_group_kwargs():
    if _os.name != "nt": return {"start_new_session": True}
    return {"creationflags": (_sp.CREATE_NEW_PROCESS_GROUP |
                               getattr(_sp, "CREATE_NO_WINDOW", 0))}

def _kill_run_tree(process, force=False):
    if _os.name == "nt":
        _force = ["/F"] if force else []
        try: _sp.run(["taskkill", *_force, "/T", "/PID", str(process.pid)],
                     capture_output=True, timeout=10)
        except Exception:
            try: process.kill() if force else process.terminate()
            except OSError: pass
        return
    import signal as _signal
    try: _os.killpg(_os.getpgid(process.pid), _signal.SIGKILL if force else _signal.SIGTERM)  # windows-footgun: ok — POSIX branch of a dual-platform worker template
    except OSError:
        try: process.kill() if force else process.terminate()
        except OSError: pass

def run(cmd, timeout=120):
    _p = _sp.Popen(cmd, shell=True, stdout=_sp.PIPE, stderr=_sp.PIPE, text=True,
                   # same contract as sandbox.run: one-shot, no stdin — a prompt fails fast
                   # instead of hanging invisibly until the deadline
                   stdin=_sp.DEVNULL,
                   **_run_group_kwargs())
    try:
        _stdout, _stderr = _p.communicate(timeout=timeout)
    except _sp.TimeoutExpired as _timeout:
        _kill_run_tree(_p)
        try: _stdout, _stderr = _p.communicate(timeout=0.5)
        except _sp.TimeoutExpired as _late:
            _kill_run_tree(_p, force=True)
            try: _stdout, _stderr = _p.communicate(timeout=2)
            except _sp.TimeoutExpired:
                _stdout = _late.stdout or _timeout.stdout or ""
                _stderr = _late.stderr or _timeout.stderr or ""
        if isinstance(_stdout, bytes): _stdout = _stdout.decode("utf-8", "replace")
        if isinstance(_stderr, bytes): _stderr = _stderr.decode("utf-8", "replace")
        _partial = (_stdout or "") + (_stderr or "")
        if _partial: print(_partial, end="" if _partial.endswith("\\n") else "\\n", flush=True)
        print(f"[run timed out after {timeout}s; process tree was reaped]",
              file=_sys.stderr, flush=True)
        # Reserved child exit: ToolHost projects it as INDETERMINATE, never an ordinary failed script.
        raise SystemExit(124)
    _o = (_stdout or "") + (_stderr or "")
    return _o if _p.returncode == 0 else f"[exit {_p.returncode}]\\n{_o}"
'''

def _fn(name: str, desc: str, props: dict, req: list[str]) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": desc,
                     "parameters": {"type": "object", "properties": props, "required": req}},
    }

# The FINDINGS-capture seam. Every tool call carries a 'note' — the model's distilled conclusion
# for this turn. It rides on the call the model is ALREADY making (no extra round-trip, unlike a
# dedicated note tool) and is folded into the slice's FINDINGS tier. This is how a Markov/slice
# agent gives a REASONING model its own prior conclusions back: the slice has no transcript, so
# without it the model re-derives the situation each turn (big reasoning bursts → slow). Reasoning
# models (e.g. deepseek) emit empty message content while tool-calling, so a tool ARG — not message
# text — is the only reliable capture point.
NOTE_PROP = {
    "note": {
        "type": "string",
        "description": ("Optional — usually leave EMPTY. Fill ONLY when this call established a NEW durable FACT "
                        "(root cause, a confirmed fix, a ruled-out hypothesis, or 'task done'), in <=15 words — a "
                        "conclusion, NOT the action you're taking. Saved across turns so you never re-derive it; "
                        "routine reads/edits need no note."),
    }
}

_LEGACY_SEMANTIC_STATE_TOOLS = frozenset({
    "world_set", "world_clear", "require", "requirement_done", "supersede_requirement",
    "drop_requirement",
})

def with_note(schema: dict) -> dict:
    """Inject the 'note' arg (first, OPTIONAL) into a tool schema — the FINDINGS capture seam.
    Applied to EVERY tool the model sees, regardless of source (builtin/MCP/plugin/skill).
    Optional, not required: the model writes it only when it has a genuine durable fact, so the
    tier fills with conclusions — not the action-narration that forcing a note on every call
    produces (and which can self-reinforce loops)."""
    fn = schema.get("function") or {}
    params = fn.get("parameters") or {"type": "object", "properties": {}, "required": []}
    props = {**NOTE_PROP, **(params.get("properties") or {})}
    req = [r for r in (params.get("required") or []) if r != "note"]
    return {**schema, "function": {**fn, "parameters": {**params, "properties": props, "required": req}}}

# _IGNORE_NAMES/_IGNORE_SUFFIX/_is_ignored (the ignore-aware directory-walk primitive shared with
# repo_map) now live in sensory_cortex.py — "ignore-aware walking" is itself a SENSORY CORTEX concern
# (perception of the live filesystem). Imported at the top of this file for _t_list_files's own use below.
_LIST_CAP = 600   # bound recursive output so a huge tree can't flood the slice

# Tool-output PAGE-OUT (#74): a single tool result larger than this is written to a blob under
# .sliceagent/blobs and replaced inline by a BOUNDED head+tail view + a read_file reference — L1→L2 paging,
# NOT a cut (the full output is preserved on disk and recall-on-demand). Keeps one huge run_command /
# execute_code / terminal_read result from flooding the within-turn transcript and forcing coarse overflow.
_OUTPUT_INLINE_CAP = 32000
_OUTPUT_HEAD = 10000
_OUTPUT_TAIL = 4000

# Drop C0/C1 control bytes (keep \t \n \r) + DEL from a paged-out output, so (a) the blob is PLAIN TEXT
# and read_file's binary gate won't hexdump it on page-back, and (b) a stray NUL can't break the API call
# when the bounded head+tail rides the transcript. Only applied on the paged path (large outputs).
_CONTROL_DROP = {c: None for c in range(0x20) if c not in (0x09, 0x0a, 0x0d)}
_CONTROL_DROP[0x7f] = None

def _strip_control(s: str) -> str:
    return s.translate(_CONTROL_DROP)
# Credential/secret dirs the shell-path auto-grant (#31) must never widen file-tool reach into.
_SECRET_DIRS = set(SENSITIVE_DIR_NAMES)

class BinaryTextError(ValueError):
    """A text-edit request targeted bytes that cannot be safely round-tripped as text."""

TOOL_SCHEMAS = [
    _fn("read_file",
        "Read a file's contents with cat -n line numbers for reference (the leading number is NOT part of the "
        "file, so don't include it in a str_replace old_string). A large file returns a bounded window with a "
        "<system> footer giving the total line count and how to page; pass `offset` (1-based start line) and/or "
        "`limit` (max lines) to read a specific range. To list a directory use list_files; to SEARCH file "
        "contents use the `grep` tool (ripgrep-backed) — not bash grep. "
        "Arg `path` may be relative to the current project, an exact absolute target under the user's home, or a "
        "read-only @sliceagent/ internal-context handle; start at @sliceagent/index.md. Grounded external targets "
        "remain reachable as focus roots. "
        "A binary file returns a hexdump preview, not editable text.",
        {"path": {"type": "string"},
         "offset": {"type": "integer", "description": "1-based first line to read (optional)"},
         "limit": {"type": "integer", "description": "max number of lines to return (optional)"}},
        ["path"]),
    _fn("list_files",
        "List directory entries (ignore-aware: skips .git/.venv/caches/build/node_modules noise). Use to "
        "discover what exists; use read_file for a file's CONTENTS and the `grep` tool (ripgrep-backed) to "
        "SEARCH text. Pass recursive=true to map a whole subtree in ONE call (flat file paths, capped at 600 — "
        "pass a subdir to narrow) — PREFER this over shell `find` for a clean cache-free map.",
        {"path": {"type": "string"}, "recursive": {"type": "boolean"}}, []),
    _fn("change_workspace",
        "Switch SliceAgent to a DIFFERENT project/workspace when the user explicitly asks to go to, open, "
        "or work in another directory. `path` must be an existing directory (discover it first if needed). "
        "This schedules a safe in-process handoff after the current turn is durably saved; every tool, index, "
        "plugin, MCP server, log, and primary-project view is rebuilt from the new directory while the same logical "
        "request and model connection continue. PROJECT-memory scope changes; USER/CRAFT memory stays available. Call this as the "
        "FINAL tool action, then briefly say the switch is happening and finish the turn.",
        {"path": {"type": "string", "description": "absolute path, ~ path, or current-workspace-relative directory"}},
        ["path"]),
    _fn("edit_file",
        "Create a new file, or OVERWRITE an existing file's ENTIRE contents with `content` (the complete text); "
        "parent dirs are auto-created and a leading `#!` shebang makes it executable. To change PART of an "
        "existing file use str_replace; to add to its end use append_to_file. Do NOT use edit_file to tweak a "
        "file — it discards all current content.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("append_to_file",
        "Append `content` to the END of a file (creates it + parent dirs if missing), preserving an existing "
        "file's dominant CRLF line endings — the only writer "
        "that ADDS without touching existing content. Use str_replace to modify text already in the file, "
        "edit_file to replace the whole file. No newline is added — include a leading '\\n' yourself if needed.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("code_review",
        "Review code changes: returns the `git diff` for the workspace (default vs HEAD; pass `ref` for a "
        "branch / commit / range like 'main', 'HEAD~3', or 'main...HEAD') so you can audit the changes for "
        "correctness, security, and edge cases — cite file:line for each issue. Read-only; needs a git repo. "
        "Prefer this over piecing a review together from many read_file calls. CALIBRATE severity: reserve "
        "critical/high for a real bug that fires in normal use or is exploitable by UNTRUSTED input — read the "
        "adjacent comment (a documented tradeoff is not a bug), trace tainted data to its real consumer before "
        "claiming a leak, remember this is a single-user LOCAL tool (self-edited config / same-user files are "
        "trusted), and report each finding only with a concrete inputs→wrong-outcome you actually traced. For "
        "a big or multi-area review, spawn_agent(agent=\"reviewer\", …) — one per area — instead.",
        {"ref": {"type": "string"},
         "include_ignored": {
             "type": "boolean",
             "description": "Set true when resolving execution uncertainty: computes the complete ignored-file manifest too",
         }}, []),
    _fn("str_replace",
        "Make a SURGICAL edit to an EXISTING file — replace one snippet, leave the rest. The default for "
        "changing a file you've read. `old_string` should be the SMALLEST unique snippet — usually 2-4 adjacent "
        "lines, not 10+. It must identify exactly ONE place: more than one occurrence is rejected (add "
        "surrounding context, or pass replace_all=true to change EVERY occurrence); an exact match is used, "
        "else a unique whitespace-tolerant fuzzy match. If old_string isn't found the file may be STALE — "
        "re-read it and copy the current text rather than retrying the same edit; for a bigger change use edit_file.",
        {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
         "replace_all": {"type": "boolean", "description": "replace ALL occurrences (default false: a >1 match is rejected)"}},
        ["path", "old_string", "new_string"]),
    _fn("run_command",
        "Run a shell command (blocking, cwd=workspace root); returns combined stdout+stderr (exit code on "
        "failure). Pass timeout (seconds, default 120, max 600) for slow builds. Use for one-shot commands that "
        "finish; for a process that must STAY alive use proc_start, for an interactive REPL use terminal_open, "
        "to chain several edits + a test in one turn use execute_code. No cwd arg — prepend `cd DIR &&`. The "
        "host records grounded paths used outside the primary workspace so file tools can re-observe them. If a command could "
        "emit a LARGE dump (disassembly, a long log, a dataset), FILTER it in the command itself — pipe "
        "through grep/head/tail/sed -n or target a range — so only the relevant slice returns.",
        {"command": {"type": "string"}, "timeout": {"type": "number"}}, ["command"]),
    _fn("execute_code",
        "Run a Python script that does SEVERAL file/shell steps in ONE turn (e.g. multiple edits + a test). Use "
        "over run_command when you'd chain many calls; over proc_start when it's one-shot. Blocking: pass "
        "timeout (seconds, default 120, max 600) when the script builds or runs a slow suite, since the "
        "deadline can otherwise land BETWEEN two edits. "
        "Helpers (no imports): read_file(path), write_file(path, content), append_file(path, content), "
        "str_replace(path, old, new), list_files(path='.'), run(shell_cmd, timeout=120). Workspace is cwd + on "
        "sys.path. ONLY "
        "what you print() is returned. The Python file helpers operate in the primary workspace; use the ordinary "
        "file tools for grounded focus roots, or run() for a shell step whose paths the host can surface afterward.",
        {"code": {"type": "string"}, "timeout": {"type": "number"}}, ["code"]),
    _fn("ask_user",
        "Ask the user a concise follow-up question and WAIT for their answer (returned to you). Use this "
        "whenever you are UNSURE or the request is AMBIGUOUS, or when you have FAILED / been blocked and don't "
        "know how to proceed — instead of guessing or repeating a failing action; prefer just answering in text "
        "when you can infer intent. Give a few short 'options' for multiple-choice, or omit for open-ended. In "
        "headless/eval runs there is no interactive user — it returns a fallback telling you to proceed with a "
        "stated assumption, so never loop waiting on it.",
        {"question": {"type": "string"},
         "options": {"type": "array", "items": {"type": "string"}}}, ["question"]),
    _fn("proc_start",
        "Start a LONG-RUNNING / background process (a server, a watcher, a multi-minute build) and return a "
        "handle (p1, p2, …) immediately; it keeps running across turns. Use over run_command when the process "
        "must outlive the turn, over terminal_open when you only launch-and-probe (it gets no stdin). It does "
        "NOT confirm the process started — one that instantly dies still returns a handle — so "
        "proc_poll/proc_tail to check status and proc_kill to stop.",
        {"command": {"type": "string"}}, ["command"]),
    _fn("proc_poll", "Check a background process by handle: 'running' or 'exited <code>'.",
        {"handle": {"type": "string"}}, ["handle"]),
    _fn("proc_tail", "Read recent output (stdout+stderr) of a background process.",
        {"handle": {"type": "string"}, "lines": {"type": "number"}}, ["handle"]),
    _fn("proc_wait",
        "Wait up to timeout seconds for a background process to exit; returns its status + recent output.",
        {"handle": {"type": "string"}, "timeout": {"type": "number"}}, ["handle"]),
    _fn("proc_kill", "Terminate a background process and its child group.",
        {"handle": {"type": "string"}}, ["handle"]),
    _fn("terminal_open",
        "Open a persistent interactive PTY session for anything needing a LIVE terminal across turns: a "
        "REPL/text-game/TUI, answering successive prompts, or holding shell state (cd/export/venv). Unlike "
        "proc_start (no stdin) or run_command (one-shot), you drive it with terminal_send/terminal_wait/"
        "terminal_read and end with terminal_close. Omit command for a shell, or pass one (e.g. 'python3 -i -q'); "
        "'session' names it (default 'main'). Don't reopen an already-open session name — close it first.",
        {"session": {"type": "string"}, "command": {"type": "string"}}, []),
    _fn("terminal_send",
        "Send input to a terminal session. By default a newline is appended (sends a line). Set "
        "enter=false to send raw keys without a newline (e.g. a control char like '\\u0003' for Ctrl-C, "
        "or an escape sequence). Returns the immediate echo/output.",
        {"session": {"type": "string"}, "input": {"type": "string"}, "enter": {"type": "boolean"}},
        ["input"]),
    _fn("terminal_read", "Read the output a terminal session has produced (drains the live stream).",
        {"session": {"type": "string"}, "timeout": {"type": "number"}}, []),
    _fn("terminal_wait",
        "Wait until a regex pattern appears in a terminal session's output (or timeout) — the reliable "
        "way to sync: send a command, then wait for its prompt/result before sending the next.",
        {"session": {"type": "string"}, "until": {"type": "string"}, "timeout": {"type": "number"}},
        ["until"]),
    _fn("terminal_close", "Close a terminal session and kill its process group.",
        {"session": {"type": "string"}}, []),
    _fn("world_set",
        "Save DURABLE task state to your WORLD MODEL under a key (overwrites that key). Use it to maintain "
        "non-code state across turns: an explored maze map, a game's rooms+inventory, a system "
        "inventory, a running plan. It appears in the WORLD MODEL section of your context from your NEXT "
        "turn on; within THIS turn, re-read a value from your own world_set call above. value may be multiline.",
        {"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]),
    _fn("world_clear", "Remove a key from your WORLD MODEL (omit key to clear all of it).",
        {"key": {"type": "string"}}, []),
    _fn("reconcile_execution",
        "Record the observed resolution of a prior INDETERMINATE operation after checking the relevant live "
        "workspace/process target. For an opaque external target, ask the user when their confirmation is the "
        "only available evidence. This clears the advisory uncertainty marker; it is not required before "
        "ordinary work or workspace/task switching. Never call it from assumption or prior memory.",
        {"resolution": {"type": "string", "description": "evidence-backed observed final state"}},
        ["resolution"]),
    _fn("require",
        "Record a STANDING REQUIREMENT that must HOLD when the task is done — an exact name/signature, an "
        "output format, a stated rule, or a constraint the user adds. It joins your STANDING REQUIREMENTS "
        "contract (shown every turn from your next turn on, and the bar for 'done'). The host already captures "
        "clauses in CURRENT REQUEST / ACTIVE USER INTENT: DO NOT call this tool to mirror those clauses. Record "
        "only a distinct durable agent-maintained constraint, never transient sub-steps or chit-chat; re-recording "
        "the same one is a no-op.",
        {"text": {"type": "string"}}, ["text"]),
    _fn("requirement_done",
        "Mark a STANDING REQUIREMENT satisfied (after verifying it against the real end-state). It stays "
        "shown as '[x] done' so it is not re-flagged but not forgotten. `text` must match the requirement.",
        {"text": {"type": "string"}}, ["text"]),
    _fn("supersede_requirement",
        "Replace an existing user-authored requirement only when the CURRENT user message explicitly "
        "corrects or changes it. `new_text` must be an exact substring of the current request; this cannot "
        "be used for a model-authored reinterpretation.",
        {"old_text": {"type": "string"}, "new_text": {"type": "string"}}, ["old_text", "new_text"]),
    _fn("drop_requirement",
        "Defer an agent-maintained STANDING REQUIREMENT that no longer applies. This cannot retract a "
        "user-authored clause; use supersede_requirement only for an explicit correction in the current request.",
        {"text": {"type": "string"}}, ["text"]),
    _fn("update_work",
        "Maintain ACTIVE WORK for a multi-step or cross-boundary request. Add or update only concrete child "
        "work items; the host owns the exact request root and sealed delivery (and any legacy verified record). Changes are "
        "source-linked to the current request, dependency-checked, and applied atomically. Use this when work "
        "must survive another turn, workspace switch, or subagent; skip it for a simple one-step answer. For a "
        "declared staged or multi-wave plan, create the complete promised frontier before execution, including "
        "later partitions as open items—never record only the current batch while future coverage lives in prose.",
        {"expected_revision": {"type": "integer", "description": "ACTIVE WORK graph revision currently shown"},
         "changes": {"type": "array", "items": {"type": "object", "properties": {
             "id": {"type": "string", "description": (
                 "stable short CHILD work-item ID; never use the host-owned current request-root ID"
             )},
             "description": {"type": "string", "description": "concrete model-maintained task description"},
             "status": {"type": "string", "enum": [
                 "open", "in_progress", "waiting_user", "ready", "cancelled", "superseded",
             ]},
             "add_dependencies": {"type": "array", "items": {"type": "string"}},
             "verify": {"type": "array", "items": {"type": "string"},
                 "description": "commands whose exit status proves this item (host-run; fix at plan time)"},
             "done_when": {"type": "string", "description": "acceptance criterion, fixed at plan time"},
             "add_resources": {"type": "array", "items": {"type": "object", "properties": {
                 "kind": {"type": "string"}, "ref": {"type": "string"},
                 "revision": {"type": "string"}}, "required": ["kind", "ref"]}},
             "superseded_by": {"type": "string"}}, "required": ["id"]}},
        }, ["changes"]),
]

# NOTE: waiting_peer is intentionally ABSENT here. It is a HOST-managed park set only via
# WorkGraph.seal_current(peer_wait=...) because it requires typed PeerWait correlation state the
# model cannot express as a prose status (like verified/delivered, the model never sets it directly).
_MODEL_WORK_STATUSES = frozenset({
    "open", "in_progress", "waiting_user", "ready", "cancelled", "superseded",
})

_VERIFY_OSCILLATION_WINDOW = 4     # identical failure signatures within this window => stop retrying

# Shell words that name no executable, so `which` says nothing about whether the command can run.
_SHELL_NONPROGRAMS = frozenset({
    "cd", "export", "set", "unset", "source", ".", "exec", "eval", "echo", "true", "false", "test",
    "[", "if", "then", "else", "elif", "fi", "for", "while", "do", "done", "case", "esac", "return",
    "shift", "trap", "wait", "read", "local", "declare", "alias", "umask", "pushd", "popd", "time",
})

def _shell_segments(command: str) -> list[str]:
    """Split on shell sequencing operators (`&&` `||` `;` `|` newline) that appear OUTSIDE quotes.

    The bare regex split also cut inside quoted payloads — `python3 -c "import sys,time; print(1)"`
    split at the `;` inside the string and the next segment's first word (`print(1)`) was then
    mis-adjudicated as an unresolvable program. Operator detection is quote-aware; anything
    ambiguous (unbalanced quotes, escapes) is left for shlex/the shell to reject downstream.
    """
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch in ";|\n":
            segments.append("".join(current))
            current = []
        elif ch == "&" and command[i + 1:i + 2] == "&":
            segments.append("".join(current))
            current = []
            i += 1
        else:
            current.append(ch)
        i += 1
    segments.append("".join(current))
    return segments

def _unrunnable_verify_program(command: str) -> str:
    """First program in `command` that cannot run — a bare name missing from PATH, or an absolute
    path whose file does not exist — or "" if nothing is confidently unrunnable.

    A verify command is the item's ACCEPTANCE CONTRACT, and it is authored during planning — when no
    shell has run and nothing has checked that the contract is even executable. `which`/`isfile` are
    pure reads, so they are legal inside a read-only planning turn, and catching `npm`/`pytest`/`cargo`
    missing HERE costs one probe instead of a full implementation cycle that ends at an unrunnable ✓.

    Deliberately conservative — it reports a BARE name (no slash, no variable, not a shell word) that
    does not resolve on PATH, plus an ABSOLUTE path whose file does not exist (equally confident: such a
    check can never run, and reporting it as red would mint an unbounded fix loop on correct work).
    Anything it cannot resolve confidently — relative paths, variables, shell words — is left to fail at
    run time rather than blocking a plan.
    """
    for segment in _shell_segments(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            words = shlex.split(segment)
        except ValueError:                       # unbalanced quotes: not ours to adjudicate
            continue
        # step over leading VAR=value assignments (`CI=1 npm run build`)
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if not words:
            continue
        program = words[0]
        if (program in _SHELL_NONPROGRAMS or "\\" in program or "$" in program
                or program.startswith(("(", "{"))):
            continue
        if program.startswith("/"):
            # Absolute paths are adjudicable: the file's existence decides, not a later shell's cwd.
            # (A directory is unrunnable too, so `isfile` is the right probe.)
            if not os.path.isfile(program):
                return program
            continue
        if "/" in program:
            continue
        if shutil.which(program) is None:
            return program
    return ""

def _verify_failure_signature(command: str, output: str) -> str:
    """Stable signature of one failed check: the command + the normalized tail of its output."""
    tail = " ".join(str(output or "").split())[-240:]
    return f"{command}::{tail}"

def run_item_verification(candidates, runner, attempts: dict) -> tuple[frozenset, str]:
    """Host-run the acceptance checks for items landing on 'ready' (P2 of PLAN-MODE-DESIGN).

    ``candidates`` = iterable of (item_id, verify_commands). ``runner(cmd) -> (ok, output)`` is injectable
    (production: CommandOracle; tests: a stub). ``attempts`` is the per-item failure-signature history used
    for OSCILLATION detection (the forge algorithm: the same failure signature recurring within the last
    ``_VERIFY_OSCILLATION_WINDOW`` attempts means retrying is not progress — escalate to the debugger).

    Returns (green_item_ids, "") on success or (frozenset(), rejection_message) on the first failure —
    update_work is an atomic batch, so one red check rejects the whole delta LOUDLY (block-render rule:
    a failed acceptance check is a real ✗, never a quiet steer).
    """
    green = set()
    for item_id, commands in candidates:
        for command in commands:
            result = runner(command)
            ok, output = result
            if ok:
                continue
            tail = " ".join(str(output or "").split())[-400:]
            # A check that never RAN — deadline overrun, missing program — rendered no verdict. It must
            # not promote the item (there is no evidence), but it is not proof of a defect either, and
            # the two need opposite responses. Reported as red it said "fix the work", so the model
            # re-edited correct code, set ready again, hit the same wall, and the oscillation detector
            # escalated a build that simply needed more than the deadline. Indeterminate is kept out of
            # the failure history for the same reason: no verdict is not a failure signature.
            if getattr(result, "status", None) is ToolStatus.INDETERMINATE:
                return frozenset(), (
                    f"verify for {item_id!r} produced NO VERDICT: `{command}` -> "
                    f"{tail or '(no output)'}. Nothing was checked, so this says nothing about the work "
                    "— do NOT re-edit on the strength of it. Fix the CHECK itself: install what it "
                    "needs, or give the item a check that completes inside the deadline (split it, or "
                    "point it at a smaller target). If only the operator can change the budget, say so "
                    "and stop — AGENT_VERIFY_TIMEOUT is read by the host process, so exporting it from "
                    "a tool does nothing."
                )
            history = attempts.setdefault(item_id, [])
            signature = _verify_failure_signature(command, output)
            oscillating = signature in history[-_VERIFY_OSCILLATION_WINDOW:]
            history.append(signature)
            del history[:-8]
            message = (
                f"verify failed for {item_id!r}: `{command}` -> {tail or '(no output)'}. "
                "The item stays unresolved (Applied is not Verified); fix the work and set it ready again."
            )
            if oscillating:
                message += (
                    " SAME failure signature has now recurred within the last "
                    f"{_VERIFY_OSCILLATION_WINDOW} attempts - retrying this path is not progress: "
                    "spawn the 'debugger' agent with this exact output for root-cause analysis, "
                    "or report BLOCKED to the user."
                )
            return frozenset(), message
        green.add(item_id)
    return frozenset(green), ""

def build_work_delta(
    graph: WorkGraph,
    args: dict,
    *,
    logical_id: str,
    workspace_epoch: int,
    verified_ok: frozenset = frozenset(),
) -> WorkDelta:
    """Normalize the small public update_work shape into the strict immutable graph contract.

    ``verified_ok`` is HOST-ONLY: item ids whose `verify` commands the host has just run green. A change
    landing such an item on 'ready' is promoted to 'verified' — the model still cannot supply 'verified'
    directly (that guard is unchanged); only a host-run check earns the promotion (Applied ≠ Verified)."""
    if not isinstance(graph, WorkGraph):
        raise ValueError("ACTIVE WORK is unavailable")
    expected = args.get("expected_revision", graph.revision)
    if not isinstance(expected, int) or isinstance(expected, bool):
        raise ValueError("expected_revision must be an integer")
    changes = args.get("changes")
    if not isinstance(changes, list) or not changes or len(changes) > 32:
        raise ValueError("changes must contain 1..32 work-item objects")
    roots = [root for root in graph.unresolved_roots if not logical_id or root.logical_id == logical_id]
    if not roots:
        raise ValueError("no active request root is available for update_work")
    root = roots[-1]
    creates, updates = [], []
    seen: set[str] = set()
    for raw in changes:
        if not isinstance(raw, dict):
            raise ValueError("each work change must be an object")
        item_id = str(raw.get("id") or "").strip()
        if not item_id or len(item_id) > 120 or item_id in seen:
            raise ValueError("each work change needs a unique ID of at most 120 characters")
        seen.add(item_id)
        previous = graph.get(item_id)
        # Omission means "leave this field alone" for an existing record.  Defaulting every partial update to
        # ``open`` made a schema-valid resource/dependency addition accidentally attempt an illegal
        # in_progress/waiting_user/ready -> open transition.
        status = str(raw.get("status") or (previous.status if previous is not None else "open"))
        if status not in _MODEL_WORK_STATUSES:
            # One predicate covered four breaches (host-only delivered / verified / waiting_peer, and a
            # plain typo) and echoed neither the item, the value, nor the legal set — the shape of the
            # verify-contract bug. Separate the host-owned statuses from an unknown one: they need
            # different responses.
            host_owned = {"delivered": "the host sets it when the turn delivers a response",
                          "verified": "the host sets it when your verify commands run green",
                          "waiting_peer": "the host sets it when a turn parks on a peer"}
            if status in host_owned:
                raise ValueError(
                    f"work item {item_id!r}: the model cannot set delivered/verified/waiting_peer — "
                    f"{status!r} is HOST-OWNED, {host_owned[status]}. "
                    f"Set 'ready' instead and let the host promote it")
            raise ValueError(
                f"work item {item_id!r}: unknown work status {status!r}; you may set "
                f"{', '.join(sorted(_MODEL_WORK_STATUSES))}")
        add_dependencies = raw.get("add_dependencies") or []
        if not isinstance(add_dependencies, list) or any(
                not isinstance(value, str) or not value.strip() for value in add_dependencies):
            raise ValueError("add_dependencies must be a list of non-empty work-item IDs")
        resource_rows = raw.get("add_resources") or []
        if not isinstance(resource_rows, list) or len(resource_rows) > 32:
            raise ValueError("add_resources must be a list of at most 32 objects")
        resources = []
        for resource in resource_rows:
            if not isinstance(resource, dict):
                raise ValueError("each resource must be an object")
            resources.append(WorkResourceRef(
                str(resource.get("kind") or ""), str(resource.get("ref") or ""),
                workspace_epoch=int(workspace_epoch), revision=str(resource.get("revision") or ""),
            ))
        superseded_by = str(
            raw.get("superseded_by")
            or (previous.superseded_by if previous is not None else "")
        ).strip()
        if status == "superseded" and not superseded_by:
            raise ValueError("superseded work must name superseded_by")
        verify_rows = raw.get("verify")
        if verify_rows is not None and (not isinstance(verify_rows, list) or any(
                not isinstance(cmd, str) for cmd in verify_rows)):
            raise ValueError("verify must be a list of shell command strings")
        done_when = raw.get("done_when")
        if done_when is not None and not isinstance(done_when, str):
            raise ValueError("done_when must be a string")
        if previous is None:
            description = str(raw.get("description") or "").strip()
            if not description:
                raise ValueError("new work items require a non-empty description")
            host_verify_proof = ()
            host_output_proof = ()
            if status == "ready" and item_id in verified_ok:
                status = "verified"
                host_verify_proof = (WorkEvidenceRef("verify_receipt", f"host-verify:{item_id}"),)
                host_output_proof = (WorkOutputRef("verified_checks", f"host-verify:{item_id}"),)
            creates.append(WorkItem(
                id=item_id, root_id=root.id, source_refs=root.source_refs,
                description=description, status=status, logical_id=root.logical_id,
                workspace_epoch=int(workspace_epoch),
                dependencies=tuple(dict.fromkeys(add_dependencies)),
                resource_refs=tuple(dict.fromkeys(resources)), superseded_by=superseded_by,
                verify=tuple(dict.fromkeys(cmd.strip() for cmd in (verify_rows or []) if cmd.strip())),
                done_when=str(done_when or "").strip(),
                evidence_refs=host_verify_proof,
                output_refs=host_output_proof,
            ))
            continue
        if previous.kind == "request":
            if previous.id == root.id or status not in {"cancelled", "superseded"}:
                raise ValueError(
                    "update_work may only cancel/supersede an older request root; the current root is host-owned",
                )
            if status == "superseded" and superseded_by != root.id:
                raise ValueError("an older request root may be superseded only by the current request root")
            # Retiring a request also retires any peer park it holds. `peer_wait` is the typed
            # state that belongs to `waiting_peer`, so a terminal status must clear it in the same
            # transition — otherwise the biconditional fires and a peer-parked root becomes a
            # one-way door that can never be cancelled or superseded.
            updates.append(replace(
                previous, status=status, superseded_by=superseded_by, peer_wait=None,
            ))
            # Retiring a request retires its still-live ownership subtree atomically.  Leaving those children
            # unresolved below a terminal root both pollutes the frontier and lets the next request's compaction
            # silently erase work that still claimed to be active.
            updates.extend(
                replace(
                    child,
                    status="cancelled",
                    superseded_by="",
                    stop_reason=f"request_{status}",
                    peer_wait=None,
                )
                for child in graph.items
                if child.id != previous.id
                and child.root_id == previous.id
                and child.status in UNRESOLVED_STATUSES
            )
            continue
        if previous.root_id != root.id:
            # EVERY user message mints a fresh logical id (cli._mint_logical_turn_id) and therefore a
            # fresh request root, so a plan made in one turn is under an older root by the next one.
            # Ownership stays per-request on purpose — an item's source_refs and host-verify context
            # belong to the request that authorized it — but the bare rule left the model with no legal
            # forward move, so a routine multi-turn checklist died on a ✗ it could not act on. Name the
            # two moves that ARE legal; the item itself is untouched and still visible.
            raise ValueError(
                f"work item {item_id!r} belongs to an EARLIER request, and items are owned by the "
                "request that created them. Nothing is lost — the item is still on record. Two legal "
                "moves, and the SECOND is usually right: (1) create a fresh item under the current "
                "request with the same description and re-state its verify/dependencies — a new item "
                "starts unverified, so evidence already earned does not carry; or (2) in the SAME "
                "batch, supersede the earlier ROOT (superseded_by = the current root) and re-create "
                "what still matters — that retires the old request instead of leaving it unfinished "
                "forever. Do not retry this update — it cannot succeed."
            )
        extra_evidence = ()
        extra_outputs = ()
        if status == "ready" and item_id in verified_ok:
            status = "verified"
            extra_evidence = (WorkEvidenceRef("verify_receipt", f"host-verify:{item_id}"),)
            extra_outputs = (WorkOutputRef("verified_checks", f"host-verify:{item_id}"),)
        updates.append(replace(
            previous,
            description=str(raw.get("description", previous.description)).strip(),
            status=status,
            dependencies=tuple(dict.fromkeys((*previous.dependencies, *add_dependencies))),
            resource_refs=tuple(dict.fromkeys((*previous.resource_refs, *resources))),
            evidence_refs=tuple(dict.fromkeys((*previous.evidence_refs, *extra_evidence))),
            output_refs=tuple(dict.fromkeys((*previous.output_refs, *extra_outputs))),
            superseded_by=superseded_by,
            verify=(tuple(dict.fromkeys(cmd.strip() for cmd in verify_rows if cmd.strip()))
                    if verify_rows is not None else previous.verify),
            done_when=(str(done_when).strip() if done_when is not None else previous.done_when),
        ))
    return WorkDelta(expected_revision=expected, creates=tuple(creates), updates=tuple(updates))

def _plan_progress_payload(graph: WorkGraph, logical_id: str) -> dict[str, object]:
    """Project the current request's Active Work into UI-only plan position.

    The immutable graph remains the sole semantic store.  Carrying this complete projection on every
    ``work_delta`` effect lets renderers follow partial item updates without reconstructing or caching a second
    plan model.
    """
    roots = [
        root for root in graph.request_roots
        if not logical_id or root.logical_id == logical_id
    ]
    if not roots:
        return {"total": 0, "done": 0, "current": "", "current_index": 0}
    root = roots[-1]
    items = [
        item for item in graph.items
        if item.kind != "request" and item.root_id == root.id
        and item.status not in {"cancelled", "superseded"}
    ]
    # A green check is earned only by host verification. ``delivered`` remains useful semantic state, but
    # it must not inflate the verified counter or become a checkmark merely because the model says it shipped.
    done_statuses = {"verified"}
    done = sum(item.status in done_statuses for item in items)
    current = None
    for wanted in ("in_progress", "waiting_user", "waiting_peer", "open", "ready"):
        current = next((item for item in items if item.status == wanted), None)
        if current is not None:
            break
    return {
        "total": len(items),
        "done": done,
        "current": current.description if current is not None else "",
        "current_index": items.index(current) + 1 if current is not None else 0,
        "items": [
            {
                "id": item.id,
                "status": item.status,
                "description": item.description,
                "done_when": item.done_when,
                "host_verified": (
                    item.status == "verified"
                    and any(
                        ref.kind == "verify_receipt" and ref.ref == f"host-verify:{item.id}"
                        for ref in item.evidence_refs
                    )
                ),
            }
            for item in items
        ],
    }

def _default_ask_user(question: str, options) -> str:
    """Fallback when no interactive user is wired (headless/eval) — never hangs."""
    return ("(no interactive user is available to answer; proceed with your best assumption and "
            "STATE it explicitly, or stop with a clear summary of what you need)")

def _sniff_image_mime(raw: bytes) -> str | None:
    """Identify an image by MAGIC BYTES (not extension). Returns the MIME type or None if not an image."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:2] == b"BM":
        return "image/bmp"
    return None

def _numbered_window(text: str, start_line: int, end_line: int, *, ctx: int = 4, cap: int = 40) -> str:
    """A cat -n numbered snippet of `text` around [start_line..end_line] (0-based), ±ctx lines, capped at
    `cap`. Edit tools echo this POST-EDIT region back in their result so the model sees the file's CURRENT
    state in-transcript — the within-turn analog of the OPEN FILES tier (the seed is frozen mid-turn, so the
    live view must ride the tool results). Bounded by construction; never the whole file."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]                                  # drop the trailing empty from a final newline
    a = max(0, start_line - ctx)
    b = min(len(lines), max(end_line + 1 + ctx, a + 1))
    b = min(b, a + cap)
    snippet = "\n".join(f"{i:>6}\t{ln}" for i, ln in enumerate(lines[a:b], a + 1))  # cat -n, absolute line nums
    if b < len(lines):
        snippet += f"\n  … (+{len(lines) - b} more lines)"
    return snippet

_SIGNAL_CLEANUP = {"fn": None, "installed": False, "sweeping": False}


def _install_signal_cleanup(fn) -> None:
    """SIGTERM/SIGHUP must not orphan every background process: atexit does not run on a signal,
    so a plain SIGTERM leaked the whole registry (the review's Family I — and the only escape from
    a wedged turn IS a signal, so this fires on the common path). One process-wide handler running
    the LATEST host's atexit cleanup, then the conventional 128+signum exit — bounded, like Kimi
    Code's 130/143.

    A SECOND signal mid-sweep means the sender is impatient (a human mashing the terminal, a
    supervisor about to SIGKILL). Re-entering the cleanup used to hit the _closed guard and
    os._exit(143) with background groups still alive — a "graceful" code that lied about a skipped
    sweep. Now a re-entrant signal dies honestly BY the signal (wait status WIFSIGNALED, plainly
    distinguishable from a completed sweep's exit 143)."""
    _SIGNAL_CLEANUP["fn"] = fn
    if _SIGNAL_CLEANUP["installed"] or os.name == "nt":
        return

    def _handler(signum, _frame):
        if _SIGNAL_CLEANUP.get("sweeping"):
            import signal as _sig
            _sig.signal(signum, _sig.SIG_DFL)
            os.kill(os.getpid(), signum)
            os._exit(128 + signum)   # unreachable once the default disposition fires; never graceful
        _SIGNAL_CLEANUP["sweeping"] = True
        try:
            cb = _SIGNAL_CLEANUP.get("fn")
            if cb is not None:
                cb()
        except Exception:  # noqa: BLE001 — cleanup must never delay the exit
            pass
        os._exit(128 + signum)

    import signal as _signal
    _signal.signal(_signal.SIGTERM, _handler)
    _signal.signal(_signal.SIGHUP, _handler)
    _SIGNAL_CLEANUP["installed"] = True


class LocalToolHost:
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
                "plan_progress": _plan_progress_payload(next_graph, logical_id),
            },
        ),)

    def root(self) -> str:
        return self._reach.primary

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
        from .fan_in import artifact_read_coverage, artifact_view_kind, canonical_artifact_id

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
                f"hexdump (first {len(head)} bytes):\n" + "\n".join(LocalToolHost._hexrows(head)))

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

    def _timeout_escalation(self, t: float, ceiling_hit: bool, *, proc_tools: bool | None = None) -> str:
        """The remediation the model needs AT the failure. Without it a timeout reads as a dead end and the
        next step is a blind retry at the same limit — the deadline is a TOOL CHOICE, not a verdict.
        Composed from the tools the CALLER can actually use: naming proc_start to a build — or a
        scoped child — that cannot call it would send the agent to a tool it does not have
        (Family J — advice to nowhere; the review's U1 at the message level)."""
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
                f"{self._timeout_escalation(t, t >= 600.0, proc_tools=(self._proc_tools_available() and not no_adopt))}\n"
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
        from .execution import ToolStatus as _TS
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
                    f"{self._timeout_escalation(t, t >= 600.0)}\n"
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
