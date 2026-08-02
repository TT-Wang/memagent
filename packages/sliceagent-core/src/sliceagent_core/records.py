"""Append-only records journal.

A durable, per-session, TYPED event log that sits ABOVE the kernel: replay/resume and the cron /
background subsystems read it. It NEVER feeds the live slice — replay rebuilds state on RESUME only,
never mid-turn (preserving the Markov boundary; cf. the records-replay moat-conflict note). It reuses a
per-session JSONL encoding also used by the legacy episode mirror; that physical resemblance does not make
either journal a separate memory layer.

`UsageRecorder` is the first consumer: it journals per-turn token usage as a durable cost log — distinct
from the in-memory `CostMetrics` summary (metrics.py), which measures the moat curve within a run.
"""
from __future__ import annotations

import json
import os

from .context import Fidelity
from .events import Event, StepEnd, ToolResult, TurnEnd, TurnInterrupted
from .private_state import open_private_append, private_dir, private_file
from .recovery import state_dir

# Records live in the sliceagent STATE dir (~/.sliceagent/records), NOT scratch/ in the user's workspace —
# the session_id is already in each filename, so a flat per-session journal needs no per-workspace key.
RECORDS_ROOT = state_dir("records")


def _records_path(session_id: str, root: str = RECORDS_ROOT) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (session_id or "default"))
    return os.path.join(root, f"{safe}.jsonl")


class Journal:
    """A per-session append-only typed-record log. `record(type, **data)` appends one line;
    `read(type=None)` reads them back (optionally filtered by type). Robust by construction: a malformed
    line is skipped, a missing file reads as empty — a journal hiccup never breaks the caller."""

    def __init__(self, session_id: str, root: str = RECORDS_ROOT):
        self.path = _records_path(session_id, root)
        parent = os.path.dirname(self.path)
        if parent and parent != ".":
            private_dir(parent)
        if os.path.exists(self.path):
            private_file(self.path)

    def record(self, rtype: str, **data) -> None:
        with open_private_append(self.path) as f:
            f.write(json.dumps({"type": rtype, **data}, ensure_ascii=False) + "\n")

    def read(self, rtype: str | None = None) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        private_file(self.path)
        out: list[dict] = []
        with open(self.path, encoding="utf-8", errors="replace") as f:   # truncated multibyte → replacement char (then json.loads skips it); never crash replay
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001 — a corrupt line never breaks replay
                    continue
                if rtype is None or rec.get("type") == rtype:
                    out.append(rec)
        return out


class UsageRecorder:
    """Event sink that journals per-turn token usage (durable cost log). Records on TurnEnd. Pure
    observer — off the moat, like CostMetrics; the difference is this PERSISTS for cross-run analysis."""

    def __init__(self, journal: Journal, model: str = ""):
        self.journal = journal
        self.model = model
        self._turn = 0
        self._acc = {"input_other": 0, "input_cache_read": 0, "input_cache_creation": 0, "output": 0}

    def __call__(self, e: Event) -> None:
        # #55: the TYPED breakdown (input_other/cache_read/…) lives on StepEnd, not on TurnEnd (whose usage
        # is just the prompt/completion totals). Accumulate per step and snapshot at turn close, so the
        # journalled cache fields are real, not always 0. Snapshot on BOTH clean and parked turn-ends.
        if isinstance(e, StepEnd):
            u = e.usage or {}
            for k in self._acc:
                self._acc[k] += u.get(k, 0) or 0
            if "output" not in u:   # legacy usage dicts: fall back to completion_tokens for output
                self._acc["output"] += u.get("completion_tokens", 0) or 0
        elif isinstance(e, (TurnEnd, TurnInterrupted)):
            self._turn += 1
            u = getattr(e, "usage", None) or {}   # TurnInterrupted carries no usage; accumulator has it
            # prefer the per-step accumulator; fall back to a typed field carried on the TurnEnd usage
            # itself (back-compat for callers that pass the full breakdown there).
            typed = {k: (self._acc[k] or u.get(k, 0) or 0) for k in self._acc}
            # On a PARKED turn (TurnInterrupted carries no usage) the prompt/completion totals would record
            # as 0 — fall back to the per-step accumulator so the journal isn't undercounted.
            acc_prompt = self._acc["input_other"] + self._acc["input_cache_read"] + self._acc["input_cache_creation"]
            self.journal.record(
                "usage", turn=self._turn, model=self.model,
                prompt_tokens=u.get("prompt_tokens") or acc_prompt,
                completion_tokens=u.get("completion_tokens") or self._acc["output"],
                **typed,
            )
            self._acc = {k: 0 for k in self._acc}


class AdmissionMetrics:
    """Event sink that journals the ADMISSION-PRECISION axis — the durable sibling of UsageRecorder's
    cost axis. Two row types per turn:

      admission    — one per SELECTED context block: {turn, block, fidelity, chars, degraded,
                     mandatory, matchable, sources}
      turn_regions — one summary: {turn, ended, admitted, degraded, referenced, unmatchable, derefs,
                     missed_need:{io delta, pageins, corrections_superseded, missing_source,
                     deref_of_degraded}}

    SOUNDNESS: 'referenced' is a deref join ONLY — a `resource_observed` tool effect whose handle or
    artifact id belongs to an admitted block's handles/resource_refs. Prose string-matching is unsound
    in both directions and is never used; a block with no handle at all lands in 'unmatchable', not
    'unreferenced'. FRAMING: on coding turns LOW deref is the HEALTHY push-dominant regime (resident
    context used without a re-read) — referenced/admitted is a re-observation-rate lower bound, never
    a utility score, and the missed-need counters are its mandatory counterweight; a deletion decision
    must read both. `missed_need.deref_of_degraded` is the strongest single signal: the compiler
    degraded something the model then had to fetch back.

    Pure observer off the moat. Providers are duck-typed closures from the host (this turn's SeedPlan
    and the live Slice); every read is best-effort — metrics must never break a turn boundary. io /
    correction counters are cumulative on the Slice, so rows carry per-turn DELTAS; the first
    observation seeds the baseline (a restored task's pre-existing counters never spike turn 1)."""

    _PAGEIN_KINDS = ("history", "artifact", "subagent", "internal_context")

    def __init__(self, journal: Journal, plan_provider=None, slice_provider=None):
        self.journal = journal
        self._plan = plan_provider
        self._slice = slice_provider
        self._turn = 0
        self._derefs: list[dict] = []
        self._io_prev: dict | None = None
        self._superseded_prev: int | None = None

    @staticmethod
    def _safe(provider):
        try:
            return provider() if provider is not None else None
        except Exception:  # noqa: BLE001 — observer: a broken provider never breaks the turn
            return None

    def __call__(self, e: Event) -> None:
        if isinstance(e, ToolResult):
            outcome = getattr(e, "outcome", None)
            for eff in getattr(outcome, "effects", ()) or ():
                if getattr(eff, "kind", "") != "resource_observed":
                    continue
                p = dict(getattr(eff, "payload", {}) or {})
                self._derefs.append({
                    "handle": str(p.get("handle") or ""),
                    "artifact_id": str(p.get("artifact_id") or ""),
                    "resource_kind": str(p.get("resource_kind") or ""),
                })
        elif isinstance(e, (TurnEnd, TurnInterrupted)):
            # BOTH endings (metrics.py #56 precedent): a parked turn must not corrupt the series.
            try:
                self._close_turn(ended="end" if isinstance(e, TurnEnd) else "interrupted")
            except Exception:  # noqa: BLE001 — observer: never let metrics break a turn boundary
                pass
            self._derefs = []

    def _close_turn(self, *, ended: str) -> None:
        self._turn += 1
        plan = self._safe(self._plan)
        selection = getattr(plan, "last_selection", None)
        blocks = tuple(getattr(selection, "blocks", ()) or ())
        deref_handles = {d["handle"] for d in self._derefs if d["handle"]}
        # Region locators spell artifact handles as artifacts/<id>.md; effects carry the canonical id.
        deref_artifacts = {f"artifacts/{d['artifact_id']}.md" for d in self._derefs if d["artifact_id"]}
        admitted: list[str] = []
        degraded: list[str] = []
        referenced: list[str] = []
        unmatchable: list[str] = []
        deref_of_degraded: list[str] = []
        for b in blocks:
            handles = set(getattr(b, "handles", ()) or ())
            handles.update(str(getattr(r, "handle", "") or "") for r in getattr(b, "resource_refs", ()) or ())
            handles.discard("")
            is_degraded = getattr(b, "fidelity", None) is not Fidelity.FULL
            matched = bool(handles & deref_handles) or bool(handles & deref_artifacts)
            item = str(getattr(b, "item_id", "") or "")
            admitted.append(item)
            if is_degraded:
                degraded.append(item)
            if not handles:
                unmatchable.append(item)
            elif matched:
                referenced.append(item)
                if is_degraded:
                    deref_of_degraded.append(item)
            source_kinds = {str(getattr(s, "kind", "") or "") for s in getattr(b, "source_refs", ()) or ()}
            source_kinds |= {str(getattr(getattr(r, "kind", None), "value", "") or "")
                             for r in getattr(b, "resource_refs", ()) or ()}
            source_kinds.discard("")
            self.journal.record(
                "admission", turn=self._turn, block=item,
                fidelity=str(getattr(getattr(b, "fidelity", None), "value", "") or ""),
                chars=len(getattr(b, "content", "") or ""), degraded=is_degraded,
                mandatory=bool(getattr(b, "mandatory", False)),
                matchable=bool(handles), sources=sorted(source_kinds),
            )
        self.journal.record(
            "turn_regions", turn=self._turn, ended=ended,
            admitted=admitted, degraded=degraded, referenced=referenced, unmatchable=unmatchable,
            derefs=len(self._derefs), missed_need=self._missed_need(blocks, deref_of_degraded),
        )

    def _missed_need(self, blocks, deref_of_degraded: list[str]) -> dict:
        s = self._safe(self._slice)
        io = dict(getattr(s, "io", {}) or {})
        prev = self._io_prev if self._io_prev is not None else io   # first sight seeds the baseline
        io_delta = {k: max(0, int(io.get(k, 0) or 0) - int(prev.get(k, 0) or 0))
                    for k in ("hit", "miss", "refault", "evict")}
        self._io_prev = io
        entries = getattr(getattr(s, "intent", None), "entries", None) or ()
        superseded_now = sum(1 for en in entries if str(getattr(en, "status", "")) == "superseded")
        superseded_prev = self._superseded_prev if self._superseded_prev is not None else superseded_now
        self._superseded_prev = superseded_now
        try:
            from .context_compiler import SOURCE_UNAVAILABLE_MARKER as marker
        except Exception:  # noqa: BLE001
            marker = "exact source: UNAVAILABLE"
        return {
            **io_delta,
            "pageins": {k: n for k in self._PAGEIN_KINDS
                        if (n := sum(1 for d in self._derefs if d["resource_kind"] == k))},
            "corrections_superseded": max(0, superseded_now - superseded_prev),
            "missing_source": sum(str(getattr(b, "content", "") or "").count(marker) for b in blocks),
            "deref_of_degraded": deref_of_degraded,
        }


def total_usage(journal: Journal) -> dict:
    """Aggregate the journal's usage records into per-model + grand totals (a simple cost report)."""
    fields = ("prompt_tokens", "completion_tokens", "input_other", "input_cache_read",
              "input_cache_creation", "output")   # #55: aggregate the cache breakdown, not just prompt/compl
    by_model: dict[str, dict] = {}
    for r in journal.read("usage"):
        m = by_model.setdefault(r.get("model") or "?", {**{f: 0 for f in fields}, "turns": 0})
        for f in fields:
            m[f] += r.get(f, 0) or 0
        m["turns"] += 1
    return by_model
