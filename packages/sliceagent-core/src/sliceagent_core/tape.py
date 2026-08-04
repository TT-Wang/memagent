"""Session Tape — the single append-only frozen stream (docs/SESSION-TAPE-DESIGN.md).

One chronological interleave of typed entries: turn digests (the spine's renderer, reused),
file BASE versions (full body, rendered once), host-authored PATCHES (the TRUE diff of the
edit the host itself applied — captured from disk at event time, never replayed from tool
args), and EXTERNAL notices (a tracked file changed outside the recorded edits). Entries are
rendered ONCE when appended and are frozen bytes forever; consumers concatenate verbatim
(R1/R3 discipline, inherited from the spine).

Composition contract (what the model is told): current content of a tracked file = its latest
`base` + every later `patch` (unified diffs), in tape order. Each patch carries the
post-composition hash; the OPEN FILES index shows the CURRENT on-disk hash — string-equal
means composition is current.

Re-base is REACTIVE only (v1.1, owner decision 2026-08-05): it happens when a fresh base is
simply the smaller representation (a rewrite whose diff would exceed the body) or when the
honesty net catches an out-of-band change. There is NO chain-length trigger — the Kimi wire
audit showed long edit chains compose fine, and error-driven correction (a failed str_replace
-> re-read) plus the per-seal honesty net catch real composition failures without a
preemptive timer. Dead bytes from re-bases are generational-compaction's job (P8).
"""
from __future__ import annotations

import difflib
import hashlib


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def render_tape_base(path: str, body: str) -> str:
    lines = body.splitlines()
    return (f"[base {path} @sha256:{_h(body)} · {len(lines)} lines]\n"
            + "\n".join(f"{i + 1:>6}\t{line}" for i, line in enumerate(lines))
            + f"\n[end base {path}]\n")


def unified_patch(path: str, before: str, after: str) -> str:
    """The TRUE delta of a host-applied edit, as a deterministic unified diff."""
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=2,
    ))


def render_tape_patch(path: str, diff: str, post_hash: str) -> str:
    return f"[patch {path} -> @sha256:{post_hash} · unified diff of the edit you made]\n{diff}\n"


def render_tape_external(path: str, new_hash: str, reason: str) -> str:
    return (f"[external {path} -> @sha256:{new_hash} — {reason}; the base below is the "
            "current truth]\n")


class TapeRecorder:
    """Collects (kind, path, disk-snapshot) at TOOL-EVENT time — the only moment each edit's
    post-state is individually observable (a seal-time disk read collapses a turn's edits)."""

    _EDITS = frozenset({"str_replace", "edit_file", "append_to_file", "write_file", "create_file"})

    def __init__(self, tools):
        self.tools = tools
        self.rows: list[tuple[str, str, str | None]] = []   # (kind, path, snapshot|None)

    def _disk(self, path: str) -> str | None:
        try:
            rd = getattr(self.tools, "resolve_read", None) or getattr(self.tools, "locate", None)
            return self.tools.read_text(rd(path) if rd else path)
        except Exception:  # noqa: BLE001 — missing/binary/unreadable: not tape-trackable
            return None

    def sink(self, event) -> None:
        if type(event).__name__ != "ToolResult" or getattr(event, "status", "") != "succeeded":
            return
        name = getattr(event, "name", "")
        args = getattr(event, "args", {}) or {}
        path = str(args.get("path") or "")
        if not path:
            return
        if name == "read_file" or name in self._EDITS:
            self.rows.append(("read" if name == "read_file" else "edit", path, self._disk(path)))

    def reset(self) -> None:
        self.rows = []


def tape_seal_update(s, tools, rows, *, session_id: str, artifact_id: str, task_id: str,
                     status: str, user_request: str) -> dict:
    """Append this sealed turn's entries to ``s.continuity.session_tape``.

    ``rows`` = TapeRecorder rows in execution order. Composition state lives in
    ``s.continuity.tape_files`` (path -> {hash, content}) on the REDACTED lane; because patches
    are true event-time diffs, replay is an identity and drift can only mean an out-of-band
    change. Returns liveness: {"entries", "drift", "rebased"}.
    """
    from .safety import redact_text
    from .spine import render_turn_digest

    tape: list = s.continuity.session_tape
    files: dict = s.continuity.tape_files
    touched: list[str] = []
    drift = 0
    rebased: list[str] = []

    def _append_base(path: str, body_r: str) -> None:
        tape.append(render_tape_base(path, body_r))
        files[path] = {"hash": _h(body_r), "content": body_r}

    tape.append(render_turn_digest(
        artifact_id=artifact_id, session_id=session_id, task_id=task_id,
        status=status, user_request=user_request,
    ))

    for kind, path, snapshot in rows:
        if snapshot is None:
            continue
        body_r = redact_text(snapshot, code_file=True)
        if path not in files:
            # first observation (read or edit of an untracked file) -> base
            _append_base(path, body_r)
            touched.append(path)
            continue
        if kind == "read":
            continue
        touched.append(path)
        state = files[path]
        if state["hash"] == _h(body_r):
            continue                       # idempotent edit / no byte change
        diff = unified_patch(path, state["content"], body_r)
        # representation choice, not policy: whichever is smaller carries the new version
        if len(diff) < len(body_r):
            state.update(content=body_r, hash=_h(body_r))
            tape.append(render_tape_patch(path, diff, state["hash"]))
        else:
            rebased.append(path)
            _append_base(path, body_r)

    # honesty net: every tracked file must match its last recorded state NOW; a mismatch is an
    # out-of-band change (shell/script/user) — re-anchor loudly.
    for path in sorted(files):
        try:
            rd = getattr(tools, "resolve_read", None) or getattr(tools, "locate", None)
            body = tools.read_text(rd(path) if rd else path)
        except Exception:  # noqa: BLE001
            continue
        body_r = redact_text(body, code_file=True)
        if _h(body_r) != files[path]["hash"]:
            drift += 1
            reason = ("changed after your last recorded edit this turn (a command/script "
                      "modified it)" if path in touched else "changed outside the recorded edits")
            tape.append(render_tape_external(path, _h(body_r), reason))
            _append_base(path, body_r)
            if path not in rebased:
                rebased.append(path)
    return {"entries": len(tape), "drift": drift, "rebased": rebased}
