"""Session Spine — frozen per-turn digests (docs/SESSION-SPINE-DESIGN.md).

ONE renderer serves every producer (R3): the host seal path, crash recovery, and any harness.
The digest is rendered exactly once per sealed segment from journal-derivable, POST-redaction
inputs (R2 — the journal header's user_request is already redacted with preserve_length=True),
embedded as a field of the artifact's structured body (R1), and from then on treated as frozen
bytes: consumers CONCATENATE stored strings verbatim and never re-render an old entry — a
renderer upgrade must never rewrite history.

Determinism contract: no timestamps, no environment-dependent values, sorted file lists, fixed
caps. Same inputs → same bytes, on any machine, in any process, forever.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

# Verbatim means never-PARAPHRASED, not never-truncated: a giant pasted ask embeds its head
# verbatim and points at the sealed artifact for the rest (the reserve degrades through a
# locator today for the same reason). Truncation is marked, never silent.
_ASK_CAP_CHARS = 2000
_MAX_FILES = 8

# R8: the paired verbatim reserve boundary — the spine subsumes every turn OLDER than this many
# completed exchanges. ONE shared knob: both lanes (the conversation region and the graph lane's
# adjacency blocks) import this constant, so the subsumption boundary can never drift per-lane.
RESERVE_PAIRS = 2


def _session_key(session_id: str) -> str:
    """MUST stay byte-identical to contextfs.ArtifactHistoryProvider._session_key — the frozen
    locator embeds its output. Guarded by test_session_spine.test_session_key_parity, which
    imports both and compares, so drift fails fast instead of dangling every frozen locator."""
    import hashlib
    import re
    value = str(session_id or "")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        return value
    return "session-" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def render_turn_digest(
    *,
    artifact_id: str,
    session_id: str,
    task_id: str,
    status: str,
    user_request: str,
    logical_turn_id: str = "",
    segment_index: int = 0,
    segment_outcome: str = "",
    title: str = "",
    files: Iterable[str] = (),
) -> str:
    """The one digest renderer. Inputs must already be redacted (they come from the journal
    header / the artifact's own redacted fields); this function adds no redaction of its own and
    therefore must never be fed live un-redacted text."""
    aid = str(artifact_id or "unknown")
    head = f"[turn {aid} · task {task_id or 'unknown'} · {status or 'unknown'}"
    if int(segment_index or 0) > 0 or segment_outcome:
        head += f" · seg {int(segment_index or 0)}"
        if segment_outcome:
            head += f":{segment_outcome}"
    head += "]"

    if int(segment_index or 0) > 0 and logical_turn_id:
        # R5 (segment-scoped entries): a continuation segment repeats the logical turn's ask —
        # dedupe by REFERENCE, never by re-summarising user words.
        ask = f"(continuation of logical turn {logical_turn_id})"
    else:
        raw = str(user_request or "").strip()
        if len(raw) > _ASK_CAP_CHARS:
            ask = raw[:_ASK_CAP_CHARS] + f" …[+{len(raw) - _ASK_CAP_CHARS} chars in sealed turn]"
        else:
            ask = raw or "(empty request)"

    lines = [head, f"ask: {ask}"]
    t = " ".join(str(title or "").split())
    if t:
        lines.append(f"note: {t[:160]}")
    fs = sorted({str(f) for f in files if str(f).strip()})
    if fs:
        shown = ", ".join(fs[:_MAX_FILES])
        extra = f" (+{len(fs) - _MAX_FILES} more)" if len(fs) > _MAX_FILES else ""
        lines.append(f"files: {shown}{extra}")
    # R4: the machine locator cites the immutable artifact_id through the session-scoped
    # contextfs route (contextfs.py renders this exact shape); positional turn-N numbering
    # dangles after restart and shifts when any one artifact is unreadable.
    lines.append(
        f'recall: read_file("@sliceagent/history/sessions/{_session_key(session_id)}/{aid}.md")'
    )
    return "\n".join(lines) + "\n"


def load_session_spine(artifacts: Iterable, session_id: str) -> list[str]:
    """Resume path (R1): the durable spine is the scan of sealed turn artifacts — stored digest
    strings verbatim, ordered by the durable order key. `Slice.session_spine` is only a cache of
    this scan. kind=='turn' filtering also excludes recovery-minted subagent-* artifacts (their
    kind is 'subagent', persistence.py recovery)."""
    from .persistence import artifact_order_key

    rows = []
    for artifact in artifacts:
        if str(getattr(artifact, "kind", "")) != "turn":
            continue
        if str(getattr(artifact, "session_id", "")) != str(session_id):
            continue
        body = getattr(artifact, "structured_body", None)
        digest = body.get("spine_digest") if isinstance(body, Mapping) else None
        if isinstance(digest, str) and digest:
            rows.append((artifact_order_key(artifact), digest))
    rows.sort(key=lambda r: r[0])
    return [digest for _, digest in rows]


def load_session_digests(artifacts: Iterable, session_id: str | None) -> list[tuple[str, str]]:
    """(artifact_id, digest) pairs in seal order — the TYPED form of the scan, so tape
    reconciliation (pre-tape/torn-journal migration) never re-parses digest strings for ids.
    ``session_id=None`` skips the session filter: a NORMAL restart mints a fresh session id
    (review Task148 blocker 1), so restart migration scopes by TASK membership instead — the
    caller passes the pre-filtered artifact list that hydrated its task checkpoints."""
    from .persistence import artifact_order_key

    rows = []
    for artifact in artifacts:
        if str(getattr(artifact, "kind", "")) != "turn":
            continue
        if session_id is not None and str(getattr(artifact, "session_id", "")) != str(session_id):
            continue
        body = getattr(artifact, "structured_body", None)
        digest = body.get("spine_digest") if isinstance(body, Mapping) else None
        if isinstance(digest, str) and digest:
            rows.append((artifact_order_key(artifact), str(artifact.id), digest))
    rows.sort(key=lambda r: r[0])
    return [(aid, digest) for _, aid, digest in rows]
