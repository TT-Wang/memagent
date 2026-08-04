"""Session Tape — the single append-only frozen stream (docs/SESSION-TAPE-DESIGN.md).

One chronological interleave of typed entries: turn digests (the spine's renderer, reused),
file BASE versions (full body, rendered once), host-authored PATCHES (the exact edit the host
applied — the model never generates these), and EXTERNAL notices (a tracked file changed
outside the recorded edits). Entries are rendered ONCE when appended and are frozen bytes
forever; consumers concatenate verbatim (R1/R3 discipline, inherited from the spine).

Composition contract (what the model is told): current content of a tracked file = its latest
`base` + every later `patch`, in tape order. Each patch carries the post-composition hash; the
OPEN FILES index shows the CURRENT on-disk hash — string-equal means composition is current.

Honesty net: at every seal the host re-composes each tracked file and byte-compares against
disk. Any drift (fuzzy-matched edits, shell writes, external changes) appends an `external`
notice plus a fresh `base` (re-base) — the tape corrects itself loudly instead of lying.
"""
from __future__ import annotations

import hashlib

# A patch bigger than this re-bases instead (a truncated patch would make composition
# impossible; a full fresh base is always safe). Chain re-base keeps dead bytes bounded until
# generational compaction (P8) removes them.
PATCH_CAP_CHARS = 1500
REBASE_CHAIN_RATIO = 1.5


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def render_tape_base(path: str, body: str) -> str:
    lines = body.splitlines()
    return (f"[base {path} @sha256:{_h(body)} · {len(lines)} lines]\n"
            + "\n".join(f"{i + 1:>6}\t{line}" for i, line in enumerate(lines))
            + "\n[end base {p}]\n".format(p=path))


def render_tape_patch(path: str, old: str, new: str, post_hash: str, *,
                      replace_all: bool = False) -> str:
    mode = "replace_all" if replace_all else "replace_once"
    return (f"[patch {path} -> @sha256:{post_hash} · {mode}]\n"
            f"<<<OLD\n{old}\nOLD===NEW\n{new}\nNEW>>>\n")


def render_tape_external(path: str, new_hash: str, reason: str) -> str:
    return (f"[external {path} -> @sha256:{new_hash} — {reason}; the base below is the "
            "current truth]\n")


class TapeRecorder:
    """Per-turn collector of successful tool rows the tape cares about (bench/host sink)."""

    def __init__(self):
        self.rows: list[tuple[str, dict]] = []

    def sink(self, event) -> None:
        name = getattr(event, "name", None)
        if (type(event).__name__ == "ToolResult" and getattr(event, "status", "") == "succeeded"
                and name in ("read_file", "str_replace", "edit_file", "append_to_file",
                             "write_file", "create_file")):
            self.rows.append((str(name), dict(getattr(event, "args", {}) or {})))

    def reset(self) -> None:
        self.rows = []


def tape_seal_update(s, tools, rows, *, session_id: str, artifact_id: str, task_id: str,
                     status: str, user_request: str) -> dict:
    """Append this sealed turn's entries to ``s.continuity.session_tape``.

    ``rows`` = the turn's successful (tool_name, args) in execution order. The composed-state
    registry lives in ``s.continuity.tape_files`` (path -> {hash, content, base_size,
    chain_bytes}); the sealed-artifact persistence of tape entries is the production follow-up —
    this in-memory form is exactly what the ONE pre-registered bench run needs.
    Returns liveness: {"entries": n, "drift": n, "rebased": [paths]}.
    """
    from .safety import redact_text
    from .spine import render_turn_digest

    tape: list = s.continuity.session_tape
    files: dict = s.continuity.tape_files
    edited_paths: list[str] = []
    drift = 0
    rebased: list[str] = []

    def _disk(path: str) -> str | None:
        try:
            rd = getattr(tools, "resolve_read", None) or getattr(tools, "locate", None)
            return tools.read_text(rd(path) if rd else path)
        except Exception:  # noqa: BLE001 — missing/binary/unreadable: not tape-trackable
            return None

    def _append_base(path: str, body: str) -> None:
        body = redact_text(body, code_file=True)
        tape.append(render_tape_base(path, body))
        files[path] = {"hash": _h(body), "content": body,
                       "base_size": len(body), "chain_bytes": 0}

    # 1. digest first (chronological head of the sealed turn's contribution)
    tape.append(render_turn_digest(
        artifact_id=artifact_id, session_id=session_id, task_id=task_id,
        status=status, user_request=user_request,
    ))

    # 2. replay the turn's successful rows in order
    for name, args in rows:
        path = str(args.get("path") or "")
        if not path:
            continue
        if name == "read_file":
            if path not in files:
                body = _disk(path)
                if body is not None:
                    _append_base(path, body)
            continue
        edited_paths.append(path)
        if name == "str_replace" and path in files:
            old = redact_text(str(args.get("old_string") or ""), code_file=True)
            new = redact_text(str(args.get("new_string") or ""), code_file=True)
            entry_size = len(old) + len(new)
            state = files[path]
            if entry_size <= PATCH_CAP_CHARS and \
                    state["chain_bytes"] + entry_size <= REBASE_CHAIN_RATIO * state["base_size"]:
                content = state["content"]
                if args.get("replace_all"):
                    composed = content.replace(old, new)
                else:
                    composed = content.replace(old, new, 1)
                state.update(content=composed, hash=_h(composed),
                             chain_bytes=state["chain_bytes"] + entry_size)
                tape.append(render_tape_patch(path, old, new, state["hash"],
                                              replace_all=bool(args.get("replace_all"))))
                continue
        # every other write shape (full overwrite, append, create, oversized/untracked edit)
        # re-bases: a full fresh body is always safe to compose from
        body = _disk(path)
        if body is not None:
            if path in files:
                rebased.append(path)
            _append_base(path, body)

    # 3. honesty net: every tracked file must byte-match its composition NOW
    for path in sorted(files):
        body = _disk(path)
        if body is None:
            continue
        body_r = redact_text(body, code_file=True)
        if _h(body_r) != files[path]["hash"]:
            drift += 1
            reason = ("edited this turn but composition drifted (fuzzy match or shell write)"
                      if path in edited_paths else "changed outside the recorded edits")
            tape.append(render_tape_external(path, _h(body_r), reason))
            _append_base(path, body)
            if path not in rebased:
                rebased.append(path)
    return {"entries": len(tape), "drift": drift, "rebased": rebased}
