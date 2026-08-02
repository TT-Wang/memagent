"""Coding-tool schemas and implementation helpers for the SliceAgent CLI."""
from __future__ import annotations

import os
import re
import shlex
import shutil

from sliceagent_core.active_work import (  # noqa: F401 — legacy compat re-exports
    MODEL_WORK_STATUSES as _MODEL_WORK_STATUSES,
    plan_progress_payload as _plan_progress_payload,
    build_work_delta as build_work_delta,
)
from sliceagent_core.execution import ToolStatus
from sliceagent_core.registry_types import ToolEntry as ToolEntry
from sliceagent_core.tool_host import (
    NOTE_PROP as NOTE_PROP,
    function_schema as _fn,
    with_note as with_note,
)

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

_LEGACY_SEMANTIC_STATE_TOOLS = frozenset({
    "world_set", "world_clear", "require", "requirement_done", "supersede_requirement",
    "drop_requirement",
})

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


_HOST_CLASS_NAMES = frozenset({"CodingToolHost", "LocalToolHost"})


def __getattr__(name: str):
    if name in _HOST_CLASS_NAMES:
        from .coding_tool_host import CodingToolHost

        return CodingToolHost
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *_HOST_CLASS_NAMES})
