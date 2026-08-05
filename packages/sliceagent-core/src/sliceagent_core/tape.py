"""Session Tape — the single append-only frozen stream (docs/SESSION-TAPE-DESIGN.md).

TYPED CORE (2026-08-05 review rebuild): the tape is a list of TapeEntry values — kind, path and
payload are STRUCTURED fields, and `rendered` is the frozen model-visible bytes produced ONCE at
append time. Every consumer that needs meaning (GC, folding, durability, honesty net) reads the
typed fields; nothing ever re-parses rendered text. The two external reviews at ed0cb69 traced
every P1 to the old shape's sidecar habits: startswith()/split() over rendered strings corrupted
paths with spaces, compaction deleted live patches while keeping their base, digests were
re-rendered from the raw (un-redacted) conversation ring, and nothing was durable.

Contracts:
- ONE producer: tape_seal_update. ONE digest render: the sealed artifact's spine_digest string is
  appended VERBATIM (callers without an artifact digest get an in-function render from redacted
  inputs — never live ring text).
- Composition: current content of a tracked file = its latest [base] + every later [patch] in
  tape order. The host-side registry (tape_files: path -> {hash, content}) holds the EXACT bytes;
  rendering normalizes a missing trailing newline for display and annotates it in the header, so
  byte-exactness never depends on the rendered form.
- Re-base is REACTIVE only (owner decision 2026-08-05): rendered-size choice per edit, honesty-net
  drift, and fold re-anchoring. No chain-length trigger (Kimi wire audit: long chains compose).
- Durability: tape_journal_append writes each sealed turn's new entries as JSONL (append-only,
  frozen bytes); load_session_tape replays the journal, rebuilds the registry by applying our own
  deterministic patches, verifies every post_hash, and compacts once to budget. A crash between
  artifact commit and journal write loses at most that turn's entries; the next seal's honesty
  net re-anchors loudly.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from dataclasses import dataclass


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


_FILE_KINDS = frozenset({"base", "patch", "external"})


@dataclass(frozen=True)
class TapeEntry:
    """One frozen tape entry. `rendered` is written once and never edited; the typed fields are
    the ONLY inputs GC/fold/durability may reason over."""

    kind: str            # "digest" | "base" | "patch" | "external" | "reply" | "epoch"
    rendered: str        # frozen model-visible bytes
    path: str = ""       # file-kind entries only
    payload: str = ""    # base: exact redacted body · patch: unified diff · others: ""
    no_nl: bool = False  # the post-state's exact content lacks a trailing newline
    post_hash: str = ""  # base/patch/external: _h() of the post-state exact content
    ref: str = ""        # digest: artifact_id · epoch: first folded ref (chain anchor)

    def to_record(self) -> dict:
        d = {"kind": self.kind, "rendered": self.rendered}
        for k in ("path", "payload", "post_hash", "ref"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.no_nl:
            d["no_nl"] = True
        return d

    @classmethod
    def from_record(cls, d: dict) -> "TapeEntry":
        return cls(kind=str(d.get("kind") or ""), rendered=str(d.get("rendered") or ""),
                   path=str(d.get("path") or ""), payload=str(d.get("payload") or ""),
                   no_nl=bool(d.get("no_nl")), post_hash=str(d.get("post_hash") or ""),
                   ref=str(d.get("ref") or ""))


def _norm(body: str) -> str:
    """Trailing-newline-normalized view used for diffing/rendering; exact bytes stay in payload."""
    return body if (not body or body.endswith("\n")) else body + "\n"


def _nl_note(body: str) -> str:
    return " · no trailing newline" if (body and not body.endswith("\n")) else ""


def render_tape_base(path: str, body: str) -> str:
    # UN-numbered (cost review 2026-08-05): cat-n numbering cost 7 chars/line and fought the
    # composition contract (patches are plain unified diffs). Line references still work: the
    # header carries the line count and hunks carry @@ offsets.
    lines = body.splitlines()
    return (f"[base {path} @sha256:{_h(body)} · {len(lines)} lines{_nl_note(body)}]\n"
            + _norm(body)
            + f"[end base {path}]\n")


def base_entry(path: str, body: str) -> TapeEntry:
    return TapeEntry(kind="base", rendered=render_tape_base(path, body), path=path,
                     payload=body, no_nl=not body.endswith("\n") if body else False,
                     post_hash=_h(body))


def unified_patch(path: str, before: str, after: str) -> str:
    """The TRUE delta of a host-applied edit, as a deterministic unified diff over
    newline-normalized views (n=1, constant a/b labels — the entry header names the path once).
    Exactness for no-trailing-newline files rides the entry's no_nl flag, not the diff text."""
    return "".join(difflib.unified_diff(
        _norm(before).splitlines(keepends=True), _norm(after).splitlines(keepends=True),
        fromfile="a", tofile="b", n=1,
    ))


def render_tape_patch(path: str, diff: str, post_hash: str, *, no_nl: bool = False) -> str:
    note = " · no trailing newline" if no_nl else ""
    return f"[patch {path} -> @sha256:{post_hash}{note}]\n{diff}\n"


def patch_entry(path: str, before: str, after: str) -> TapeEntry:
    diff = unified_patch(path, before, after)
    no_nl = not after.endswith("\n") if after else False
    return TapeEntry(kind="patch", rendered=render_tape_patch(path, diff, _h(after), no_nl=no_nl),
                     path=path, payload=diff, no_nl=no_nl, post_hash=_h(after))


def apply_unified(before: str, diff_text: str) -> str:
    """Apply one of OUR deterministic unified diffs (n=1, a/b labels) to `before`'s normalized
    view. Raises ValueError on any mismatch — callers treat that as a stale journal entry."""
    src = _norm(before).splitlines(keepends=True)
    out: list[str] = []
    pos = 0
    lines = diff_text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith(("---", "+++")):
            i += 1
            continue
        if ln.startswith("@@"):
            try:
                old_start = int(ln.split("-", 1)[1].split(",")[0].split(" ")[0])
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"bad hunk header: {ln!r}") from exc
            hunk_pos = old_start - 1
            out.extend(src[pos:hunk_pos])
            pos = hunk_pos
            i += 1
            while i < len(lines) and not lines[i].startswith("@@"):
                h = lines[i]
                if h.startswith(" "):
                    if pos >= len(src) or src[pos] != h[1:]:
                        raise ValueError(f"context mismatch at line {pos + 1}")
                    out.append(src[pos]); pos += 1
                elif h.startswith("-"):
                    if pos >= len(src) or src[pos] != h[1:]:
                        raise ValueError(f"delete mismatch at line {pos + 1}")
                    pos += 1
                elif h.startswith("+"):
                    out.append(h[1:])
                elif h.strip() == "":
                    pass                       # trailing separator inside rendered block
                else:
                    raise ValueError(f"bad hunk line: {h!r}")
                i += 1
            continue
        i += 1
    out.extend(src[pos:])
    return "".join(out)


def compose_after(entry: TapeEntry, before: str) -> str:
    """Post-state exact bytes for a base/patch entry (journal replay)."""
    if entry.kind == "base":
        return entry.payload
    after = apply_unified(before, entry.payload)
    if entry.no_nl and after.endswith("\n"):
        after = after[:-1]
    return after


def render_tape_external(path: str, new_hash: str, reason: str) -> str:
    return (f"[external {path} -> @sha256:{new_hash} — {reason}; the entry below re-anchors "
            "to the current on-disk truth]\n")


def external_entry(path: str, new_hash: str, reason: str) -> TapeEntry:
    return TapeEntry(kind="external", rendered=render_tape_external(path, new_hash, reason),
                     path=path, post_hash=new_hash)


# The reply entry replaces the RECENT CONVERSATION region under the tape (census 2026-08-05:
# that region cost 3.8k chars EVERY boundary at full price; a frozen reply entry is billed once).
# Verbatim head + loud cap; the sealed turn artifact carries the full text.
REPLY_CAP_CHARS = 1200


def render_tape_reply(artifact_id: str, text: str) -> str:
    body = str(text or "").strip()
    if len(body) > REPLY_CAP_CHARS:
        body = body[:REPLY_CAP_CHARS] + f" …[+{len(body) - REPLY_CAP_CHARS} chars in sealed turn]"
    return f"[reply {artifact_id}]\n{body}\n[end reply]\n" if body else ""


def reply_entry(artifact_id: str, text: str) -> TapeEntry | None:
    rendered = render_tape_reply(artifact_id, text)
    return TapeEntry(kind="reply", rendered=rendered, ref=str(artifact_id)) if rendered else None


def digest_entry(rendered_digest: str, artifact_id: str = "") -> TapeEntry:
    """The sealed artifact's spine_digest string, appended VERBATIM (R1: one render, and it
    inherits the seal path's redaction)."""
    return TapeEntry(kind="digest", rendered=rendered_digest, ref=str(artifact_id))


def tape_render(tape: list) -> str:
    """The model-visible stream: rendered frozen bytes, concatenated verbatim."""
    return "".join(e.rendered for e in tape)


def tape_chars(tape: list) -> int:
    return sum(len(e.rendered) for e in tape)


# Generational compaction (SESSION-TAPE-DESIGN §3): the bound is a CONTRACT. When the tape
# exceeds the budget, dead file history (entries superseded by a later base) is garbage-collected
# first; if still over, the oldest span folds into one epoch marker. Every file with ANY entry in
# the folded span is RE-ANCHORED: one fresh base rendered from the registry's CURRENT composed
# content replaces that file's whole chain (review P1: the old fold kept a stale base and deleted
# its later patches — base+patch composition then failed and every affected file forced a
# re-read). Each compaction mutates frozen bytes and therefore breaks the provider prefix ONCE —
# deliberately, rarely, and counted.
TAPE_BUDGET_CHARS = int(os.environ.get("AGENT_TAPE_BUDGET", "") or 120_000)
_FOLD_TARGET = 0.7   # fold DOWN TO this fraction of budget — hysteresis so back-to-back turns
#                      cannot re-trigger (measured thrash: s2 r3 folds at turns 9 AND 10, each a
#                      ~55k-char full re-bill of everything below the tape head)


def compact_tape(tape: list, files: dict, *, budget: int = TAPE_BUDGET_CHARS) -> dict:
    info = {"gc_removed": 0, "epoch_folds": 0}
    if tape_chars(tape) <= budget:
        return info
    # pass 1: GC dead file history — typed: drop file-kind entries strictly before their path's
    # latest base (they are superseded; the latest base + later patches carry the current truth).
    latest_base: dict[str, int] = {}
    for i, e in enumerate(tape):
        if e.kind == "base":
            latest_base[e.path] = i
    dead = {i for i, e in enumerate(tape)
            if e.kind in _FILE_KINDS and e.path in latest_base and i < latest_base[e.path]}
    if dead:
        info["gc_removed"] = len(dead)
        tape[:] = [e for i, e in enumerate(tape) if i not in dead]
    # pass 2: ONE fold sized by NET effect. Files touched inside the span are re-anchored to
    # their registry content as fresh bases — which ADDS bytes — so the cut must grow until
    # (bytes removed, including the affected files' post-cut entries) minus (marker + anchor
    # bases) actually reaches the target. The first typed fold sized the cut by span bytes
    # alone; on s11's real files each fold removed small digests/patches but appended full
    # fresh bases, never reached budget, and re-folded EVERY seal (18 folds, fresh +42%,
    # tape 166k > 120k budget — graduation gate G2 catch, 2026-08-05).
    total = tape_chars(tape)
    if total > budget and len(tape) > 8:
        target = int(budget * _FOLD_TARGET)
        def _anchor_cost(path: str) -> int:
            # cheap estimate of a fresh base's rendered size (exact render happens once, later)
            content = files.get(path, {}).get("content", "")
            return len(content) + 2 * len(path) + 80
        affected: set[str] = set()
        removed = 0
        anchors_cost = 0
        post_cut_by_path: dict[str, int] = {}
        for e in tape:
            if e.kind in _FILE_KINDS and e.path:
                post_cut_by_path[e.path] = post_cut_by_path.get(e.path, 0) + len(e.rendered)
        best_cut, best_net = 0, 0
        for cut in range(1, len(tape) - 3):
            e = tape[cut - 1]
            removed += len(e.rendered)
            if e.kind in _FILE_KINDS and e.path:
                post_cut_by_path[e.path] -= len(e.rendered)
                if e.path not in affected:
                    affected.add(e.path)
                    anchors_cost += _anchor_cost(e.path)
            # dropping an affected file's post-cut entries also reclaims their bytes
            extra = sum(post_cut_by_path[p] for p in affected)
            net = removed + extra - anchors_cost - 200      # 200 ≈ marker
            if net > best_net:
                best_cut, best_net = cut, net
            if total - net <= target:
                break
        if best_net <= 0:
            return info      # nothing reclaimable (working set alone floors the tape): bail,
        cut = best_cut       # never stack markers that grow the tape
        span = tape[:cut]
        affected = sorted({e.path for e in span if e.kind in _FILE_KINDS and e.path})
        anchors = [base_entry(p, files[p]["content"]) for p in affected if p in files]
        folded_history = sum(1 for e in span if not (e.kind in _FILE_KINDS and e.path in affected))
        refs = [e.ref for e in span if e.kind == "digest" and e.ref]
        first = refs[0] if refs else "start"
        if span and span[0].kind == "epoch" and span[0].ref:
            first = span[0].ref
        last = refs[-1] if refs else "…"
        marker = TapeEntry(
            kind="epoch", ref=first,
            rendered=(f"[epoch compacted: {first}..{last} — {folded_history} history entries "
                      "removed; re-anchored files follow as fresh bases; the full sealed record "
                      "remains readable via read_file(\"@sliceagent/history/index.md\")]\n"),
        )
        keep_tail = [e for e in tape[cut:] if not (e.kind in _FILE_KINDS and e.path in affected)]
        tape[:] = [marker, *anchors, *keep_tail]
        info["epoch_folds"] += 1
    return info


class TapeRecorder:
    """Collects (path, disk-snapshot) at EDIT tool-event time — the only moment each edit's
    post-state is individually observable (a seal-time disk read collapses a turn's edits).
    Reads are NOT recorded: defer-base-until-edit never consumes them, and snapshotting every
    read cost one full extra disk read per read_file (review note c)."""

    _EDITS = frozenset({"str_replace", "edit_file", "append_to_file", "write_file", "create_file"})

    def __init__(self, tools):
        self.tools = tools
        self.rows: list[tuple[str, str | None]] = []   # (path, post-state snapshot | None)

    def _disk(self, path: str) -> str | None:
        try:
            rd = getattr(self.tools, "resolve_read", None) or getattr(self.tools, "locate", None)
            return self.tools.read_text(rd(path) if rd else path)
        except Exception:  # noqa: BLE001 — missing/binary/unreadable: not tape-trackable
            return None

    def sink(self, event) -> None:
        if type(event).__name__ != "ToolResult" or getattr(event, "status", "") != "succeeded":
            return
        if getattr(event, "name", "") not in self._EDITS:
            return
        path = str((getattr(event, "args", {}) or {}).get("path") or "")
        if path:
            self.rows.append((path, self._disk(path)))

    def rebind(self, tools) -> None:
        """Point the recorder at a new workspace toolset (workspace handoff)."""
        self.tools = tools
        self.rows = []

    def reset(self) -> None:
        self.rows = []


def tape_seal_update(s, tools, rows, *, session_id: str, artifact_id: str, task_id: str,
                     status: str, user_request: str, assistant_reply: str = "",
                     digest_text: str = "", journal_path: str = "",
                     budget: int = TAPE_BUDGET_CHARS) -> dict:
    """THE single producer: append this sealed turn's entries to ``s.continuity.session_tape``.

    ``digest_text`` is the sealed artifact's spine_digest — appended verbatim (one render, seal
    redaction inherited). Callers without one (bench harness) get an in-function render from
    REDACTED inputs. ``rows`` = TapeRecorder rows in execution order. Returns liveness:
    {"entries", "drift", "rebased", "gc_removed", "epoch_folds"}.
    """
    from .safety import redact_text
    from .spine import render_turn_digest

    tape: list = s.continuity.session_tape
    files: dict = s.continuity.tape_files
    touched: list[str] = []
    drift = 0
    rebased: list[str] = []
    new_entries: list[TapeEntry] = []

    def _append(entry: TapeEntry) -> None:
        tape.append(entry)
        new_entries.append(entry)

    def _anchor(path: str, body_r: str, *, prev: str | None) -> None:
        """Append the smaller RENDERED representation (review P2: raw-length comparison chose a
        1.46k base over a 1.24k rendered patch)."""
        if prev is None:
            _append(base_entry(path, body_r))
        else:
            pe, be = patch_entry(path, prev, body_r), base_entry(path, body_r)
            if len(pe.rendered) < len(be.rendered):
                _append(pe)
            else:
                rebased.append(path)
                _append(be)
        files[path] = {"hash": _h(body_r), "content": body_r}

    if digest_text:
        _append(digest_entry(digest_text, artifact_id))
    else:
        _append(digest_entry(render_turn_digest(
            artifact_id=artifact_id, session_id=session_id, task_id=task_id,
            status=status, user_request=redact_text(str(user_request or "")),
        ), artifact_id))

    for path, snapshot in rows:
        if snapshot is None:
            continue
        body_r = redact_text(snapshot, code_file=True)
        state = files.get(path)
        if state is not None and state["hash"] == _h(body_r):
            touched.append(path)
            continue                       # idempotent edit / no byte change
        touched.append(path)
        _anchor(path, body_r, prev=None if state is None else state["content"])

    # honesty net: every tracked file must match its last recorded state NOW; a mismatch is an
    # out-of-band change (shell/script/user) — re-anchor loudly, delta-sized like any edit.
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
            _append(external_entry(path, _h(body_r), reason))
            _anchor(path, body_r, prev=files[path]["content"])
            if path not in rebased:
                rebased.append(path)

    # the turn's outward answer, frozen last (chronology) — replaces the RECENT CONVERSATION
    # region's per-boundary re-bill; deixis anchors against tape bytes from here on
    rep = reply_entry(artifact_id, redact_text(str(assistant_reply or "")))
    if rep is not None:
        _append(rep)

    if journal_path:
        try:
            tape_journal_append(journal_path, new_entries)
        except Exception:  # noqa: BLE001 — durability is best-effort; the live tape is intact and
            pass           # the next seal's honesty net re-anchors anything a replay would miss

    compaction = compact_tape(tape, files, budget=budget)
    return {"entries": len(tape), "drift": drift, "rebased": rebased, **compaction}


def reconcile_tape_with_digests(tape: list, digest_pairs: list,
                                *, last_reply: tuple | None = None) -> int:
    """Migration/repair seam (review Task147 blocker 1): fold ARTIFACT-TRUTH digests into the
    tape at hydration. A pre-graduation session has sealed artifacts but no tape journal — the
    spine region that used to render those digests is retired, so without this the session
    resumes with its earlier-turn asks invisible. Also covers a torn journal (crash between
    artifact commit and journal write: the missing tail turns re-enter here).

    ``digest_pairs`` = spine.load_session_digests output, seal order. Rules:
    - empty tape -> every digest appends (chronological);
    - non-empty tape -> only pairs NEWER than the newest artifact already represented append
      (never resurrect digests a fold deliberately compacted away);
    - ``last_reply`` = (artifact_id, text): the newest sealed turn's outward answer re-freezes
      as a [reply] entry unless one for that artifact already exists.
    Returns the number of entries appended. Idempotent."""
    refs = {e.ref for e in tape if e.kind == "digest" and e.ref}
    added = 0
    seen = [i for i, (aid, _d) in enumerate(digest_pairs) if aid in refs]
    if not seen:
        # no overlap (pre-tape session, or a journal that predates every listed artifact):
        # artifact truth wins — every digest enters, oldest first, BEFORE any journaled entries
        # so chronology holds. One-time prefix re-bill, on the migration turn only.
        fresh = [digest_entry(d, aid) for aid, d in digest_pairs if aid not in refs]
        tape[:0] = fresh
        added += len(fresh)
    else:
        first, last = seen[0], seen[-1]
        # pre-journal history (session upgraded mid-life): PREPEND, keeping seal order
        prepend = [digest_entry(d, aid) for aid, d in digest_pairs[:first] if aid not in refs]
        tape[:0] = prepend
        added += len(prepend)
        # gaps BETWEEN seen ids are digests a fold deliberately compacted — never resurrected.
        # tail beyond the newest seen id = torn-journal turns: APPEND.
        for aid, digest in digest_pairs[last + 1:]:
            if aid not in refs:
                tape.append(digest_entry(digest, aid))
                added += 1
    if last_reply:
        aid, text = last_reply
        has_reply = any(e.kind == "reply" and e.ref == str(aid) for e in tape)
        if not has_reply and str(text or "").strip():
            rep = reply_entry(str(aid), str(text))
            if rep is not None:
                tape.append(rep)
                added += 1
    return added


# ── Durability: append-only JSONL journal ─────────────────────────────────────────────────────
# One line per entry, in append order, written at seal time. Compaction NEVER rewrites the
# journal (it is the full history); load replays every line and compacts once at the end, so a
# reloaded session sees the same bounded tape a live one would.

def tape_journal_append(path: str, entries: list) -> None:
    if not entries:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e.to_record(), ensure_ascii=False) + "\n")


def load_session_tape(path: str, *, budget: int = TAPE_BUDGET_CHARS) -> tuple[list, dict]:
    """Rebuild (session_tape, tape_files) from the journal. Every base/patch replays through
    compose_after and is verified against its post_hash; a mismatching or unappliable entry drops
    its path from the registry (the composition contract then routes the model to read_file —
    safe degradation, and the next edit founds a fresh base)."""
    tape: list[TapeEntry] = []
    files: dict = {}
    if not os.path.isfile(path):
        return tape, files
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            e = TapeEntry.from_record(json.loads(line))
        except Exception:  # noqa: BLE001 — a torn tail line (crash mid-write) ends the replay
            break
        tape.append(e)
        if e.kind in ("base", "patch"):
            try:
                before = files.get(e.path, {}).get("content", "")
                after = compose_after(e, before)
                if _h(after) != e.post_hash:
                    raise ValueError("post_hash mismatch")
                files[e.path] = {"hash": e.post_hash, "content": after}
            except Exception:  # noqa: BLE001
                files.pop(e.path, None)
    compact_tape(tape, files, budget=budget)
    return tape, files
