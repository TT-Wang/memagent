"""Addressable discourse anchors for user-visible assistant output.

The archive keeps the full turn.  This module derives only small, source-linked
addresses for things users naturally refer to later ("number 2", "the first
subagent", "your original findings").  The anchors are a pageable index, not a
second copy of the conversation and never a source of factual truth about the
live workspace.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Mapping


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BOLD_HEADING = re.compile(r"^\s*\*\*(.+?)\*\*\s*:?[\s-]*$")
_NUMBERED = re.compile(r"^\s*\*{0,2}(\d{1,3})[.)]\*{0,2}\s+(.+?)\s*$")
_STABLE_ID = re.compile(r"\b(?:sub|agent|finding|bug)-\d+\b", re.IGNORECASE)
_PROPOSAL = re.compile(
    r"(?:^|(?<=[.!?]))\s*((?:would\s+you\s+like\s+me\s+to|"
    r"do\s+you\s+want\s+me\s+to|want\s+me\s+to|shall\s+i|should\s+i|i\s+can)"
    r"\b[^?.!\n]{1,300}\?)",
    re.IGNORECASE,
)
_CHOICE_QUESTION = re.compile(
    r"((?:which\b[^?\n]{0,100}\b(?:prefer|choose|pick)|"
    r"(?:please\s+)?(?:choose|pick)\s+(?:one|an?\s+option))[^?\n]*\?)",
    re.IGNORECASE,
)
_QUESTION_SENTENCE = re.compile(r"(?:^|(?<=[.!?]))\s*([^?\n]{1,300}\?)")
_PATH_TOKEN = re.compile(
    r"`((?:~[/\\]|/|[A-Za-z]:[/\\])[^`\r\n?]+)`|"
    r"((?:~[/\\]|/|[A-Za-z]:[/\\])[^\s?]+)"
)
_PATH_CONFIRMATION = re.compile(r"\b(?:is\s+(?:it|that)|confirm\b|correct\b|right\b)", re.IGNORECASE)
_WORKSPACE_CONTEXT = re.compile(
    r"\b(?:workspace|project|repo(?:sitory)?|directory|folder)\b", re.IGNORECASE,
)
# The assistant's own "which one should I navigate to — loom-app or loom-engine?" question names bare
# directory options but no absolute path (so the path-confirmation branch above cannot fire). A next-turn
# reply naming one option confers scoped navigation authority (resolved in intent._selected_nav_target_grant).
_NAV_DISAMBIGUATION = re.compile(
    r"\bwhich\b[^?\n]{0,120}\b(?:navigate|switch\s+to|go\s+(?:to|into)|move\s+(?:to|into)|"
    r"open|cd|chdir|work\s+in)\b", re.IGNORECASE)
_DIR_OPTION = re.compile(r"\b([A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)+)\b")
# A single-target navigation OFFER inside an assistant proposal ("do you want me to switch to loom-app?").
# The bare directory name (not an absolute path) means the path-confirmation branch cannot fire; a bare
# "yes" then continues this one navigation (intent.analyze_turn treats a single nav_target as acceptable).
_NAV_OFFER = re.compile(
    r"\b(?:navigate|switch|go|move|cd|chdir|change(?:\s+(?:the\s+)?workspace)?)\s+(?:to|into)\s+"
    r"(?:the\s+)?(?:workspace\s+|folder\s+|directory\s+|project\s+)?"
    r"([A-Za-z0-9~][A-Za-z0-9._/-]*)", re.IGNORECASE)


def _plain(text: str) -> str:
    value = re.sub(r"[`*_~]", "", str(text or ""))
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n:-")
    return value


def _proposal_scan(text: str) -> str:
    """Blank example/code spans while preserving offsets into the visible assistant response."""
    chars = list(str(text or ""))

    def blank(start: int, end: int) -> None:
        for index in range(max(0, start), min(len(chars), end)):
            if chars[index] not in "\r\n":
                chars[index] = " "

    source = str(text or "")
    offset = 0
    fence: tuple[str, int] | None = None
    for line in source.splitlines(keepends=True):
        marker = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        in_fence = fence is not None
        if marker is not None:
            token = marker.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif (token[0] == fence[0] and len(token) >= fence[1]
                  and not line[marker.end():].strip()):
                fence = None
            blank(offset, offset + len(line))
        elif in_fence:
            blank(offset, offset + len(line))
        elif re.match(r"^(?:\t| {4,}|\s*>)", line):
            blank(offset, offset + len(line))
        offset += len(line)
    # A whole question shown as quoted/inline-code data is not the assistant asking it. A quoted path inside
    # a real surrounding question remains visible because the question mark sits outside the quote.
    for pattern in (r'"[^"\r\n]*\?[^"\r\n]*"', r"'[^'\r\n]*\?[^'\r\n]*'", r"`[^`\r\n]*\?[^`\r\n]*`"):
        for match in re.finditer(pattern, source):
            blank(*match.span())
    return "".join(chars)


def extract_pending_proposal(text: str) -> dict | None:
    """Return one immediate assistant action offer, or ``None``.

    A bare user assent may inherit effect authority only from this explicit, source-linked continuity object.
    Merely mentioning that a fix exists is not a proposal.
    """
    source = str(text or "")
    scan = _proposal_scan(source)
    choices = list(_CHOICE_QUESTION.finditer(scan))
    anchors = extract_addressable_anchors(scan) if choices else ()
    option_anchors = []
    if choices and anchors:
        question = choices[-1]
        candidates = [anchor for anchor in anchors if anchor.source_range[0] < question.start(1)]
        if candidates:
            option_anchors = [candidates[-1]]
            expected = candidates[-1].ordinal - 1
            for anchor in reversed(candidates[:-1]):
                if anchor.collection != option_anchors[-1].collection or anchor.ordinal != expected:
                    break
                option_anchors.append(anchor)
                expected -= 1
            option_anchors.reverse()
    if choices and len(option_anchors) >= 2:
        question = choices[-1]
        start = option_anchors[0].source_range[0]
        return {
            "text": source[start:question.end(1)],
            "source_range": [start, question.end(1)],
            "options": [anchor.to_dict() for anchor in option_anchors],
        }
    # A clarification of the exact target for an already-requested workspace navigation is itself a typed
    # pending action. This is deliberately narrow: an arbitrary yes/no question (or even an arbitrary path
    # question) cannot confer effect authority. The surrounding assistant text must identify a workspace-like
    # frame, and the question must contain one concrete absolute/home path.
    for question in reversed(list(_QUESTION_SENTENCE.finditer(scan))):
        sentence = question.group(1)
        if _PATH_CONFIRMATION.search(sentence) is None:
            continue
        context = scan[max(0, question.start(1) - 400):question.end(1)]
        paths = list(_PATH_TOKEN.finditer(sentence))
        if not paths:
            paths = list(_PATH_TOKEN.finditer(context))
        if not paths:
            continue
        if _WORKSPACE_CONTEXT.search(context) is None:
            continue
        path_match = paths[-1]
        path = (path_match.group(1) or path_match.group(2) or "").rstrip(".,;:!)]}\"'*_~")
        if not path:
            continue
        start, end = question.span(1)
        return {
            "text": source[start:end],
            "source_range": [start, end],
            "action": {"tool": "change_workspace", "args": {"path": path}},
        }
    # A workspace-navigation disambiguation question offering named directory options. No absolute path is
    # present, so this is the naming analogue of the path-confirmation branch above: the reply naming an
    # option is a typed navigate selection, not an arbitrary yes/no continuation.
    for question in reversed(list(_QUESTION_SENTENCE.finditer(scan))):
        sentence = question.group(1)
        if _NAV_DISAMBIGUATION.search(sentence) is None:
            continue
        # A strong nav verb (navigate/switch to/cd) + >=2 directory-like options is signal enough; no
        # extra workspace-context word is required (it wrongly rejected the plural "directories").
        context = scan[max(0, question.start(1) - 400):question.end(1)]
        names: list[str] = []
        for option in _DIR_OPTION.finditer(context):
            name = option.group(1)
            if name.casefold() not in {existing.casefold() for existing in names}:
                names.append(name)
        if len(names) < 2:
            continue
        start, end = question.span(1)
        return {
            "text": source[start:end],
            "source_range": [start, end],
            "nav_targets": names,
        }
    matches = list(_PROPOSAL.finditer(scan))
    if not matches:
        return None
    match = matches[-1]
    start, end = match.span(1)
    proposal_text = source[start:end]
    # A single-target navigation offer ("do you want me to switch to loom-app?") is a typed navigate
    # selection: a bare "yes" then authorizes navigating to that one named target.
    nav = _NAV_OFFER.search(proposal_text)
    if nav is not None:
        name = nav.group(1).rstrip("?.,;:!)/'\"")
        if name:
            return {"text": proposal_text, "source_range": [start, end], "nav_targets": [name]}
    return {
        "text": proposal_text,
        "source_range": [start, end],
    }


@dataclass(frozen=True)
class DiscourseAnchor:
    """One addressable item in a sealed, user-visible assistant response."""

    collection: str
    ordinal: int
    label: str
    excerpt: str
    source_range: tuple[int, int]
    stable_id: str = ""
    artifact_id: str = ""
    task_id: str = ""
    sequence: int = 0

    def to_dict(self) -> dict:
        return {
            "collection": self.collection,
            "ordinal": self.ordinal,
            "label": self.label,
            "excerpt": self.excerpt,
            "source_range": list(self.source_range),
            "stable_id": self.stable_id,
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> "DiscourseAnchor | None":
        if not isinstance(value, Mapping):
            return None
        try:
            ordinal = int(value.get("ordinal") or 0)
        except (TypeError, ValueError):
            return None
        raw_range = value.get("source_range")
        if not (isinstance(raw_range, (list, tuple)) and len(raw_range) == 2
                and all(isinstance(item, int) for item in raw_range)):
            return None
        label = str(value.get("label") or "").strip()
        excerpt = str(value.get("excerpt") or "").strip()
        if ordinal <= 0 or not (label or excerpt):
            return None
        return cls(
            collection=str(value.get("collection") or "numbered list"),
            ordinal=ordinal,
            label=label or _plain(excerpt.splitlines()[0]),
            excerpt=excerpt or label,
            source_range=(raw_range[0], raw_range[1]),
            stable_id=str(value.get("stable_id") or ""),
            artifact_id=str(value.get("artifact_id") or ""),
            task_id=str(value.get("task_id") or ""),
            sequence=int(value.get("sequence") or 0),
        )


@dataclass(frozen=True)
class AdmissionPreview:
    """Pure, immutable result of orienting one request before any session mutation.

    The host may route first, compute this preview against the prospective task, begin its durable journal,
    then apply ``admission`` and ``focus`` exactly once. ``contract`` remains a read-only compatibility name.
    """

    admission: object
    focus: tuple[dict, ...] = ()
    referenced_artifact_ids: tuple[str, ...] = ()
    # Exact source projections are turn-ephemeral. They may accumulate elastically inside the active slice,
    # but are deliberately excluded from the admission journal and cross-session task state.
    projections: tuple[dict, ...] = ()
    # Source identities + digests needed to reconstruct the same canonical evidence on an adjacent challenge.
    # Unlike ``projections``, this contains no utterance or receipt payload bytes.
    snapshot_basis: dict | None = None
    ambiguous: bool = False
    consume_pending_proposal: bool = True

    @property
    def contract(self):
        return self.admission

    def to_dict(self) -> dict:
        admission = self.admission
        return {
            "admission": admission.to_dict() if hasattr(admission, "to_dict") else admission,
            "focus": [dict(item) for item in self.focus],
            "referenced_artifact_ids": list(self.referenced_artifact_ids),
            "projection_kinds": [str(item.get("kind") or "") for item in self.projections],
            "ambiguous": self.ambiguous,
            "consume_pending_proposal": self.consume_pending_proposal,
        }


# Compatibility alias: there is one preview object, not parallel interpretation/admission writers.


def extract_addressable_anchors(text: str) -> tuple[DiscourseAnchor, ...]:
    """Extract Markdown numbered items with exact source ranges.

    Continuation lines belong to the item until the next numbered item or heading,
    so the archived excerpt remains useful rather than retaining only a lossy title.
    """
    source = str(text or "")
    if not source.strip():
        return ()
    heading = "numbered list"
    lines = source.splitlines(keepends=True)
    anchors: list[DiscourseAnchor] = []
    active: dict | None = None
    list_indent: int | None = None
    offset = 0

    def finish(end: int) -> None:
        nonlocal active
        if active is None:
            return
        excerpt = source[active["start"]:end].strip()
        first = _NUMBERED.match(source[active["start"]:active["line_end"]].rstrip("\r\n"))
        label = _plain(first.group(2) if first is not None else excerpt.splitlines()[0])
        stable = _STABLE_ID.search(excerpt)
        anchors.append(DiscourseAnchor(
            collection=active["collection"], ordinal=active["ordinal"],
            label=label[:300], excerpt=excerpt,
            source_range=(active["start"], end),
            stable_id=stable.group(0).casefold() if stable else "",
        ))
        active = None

    for line in lines:
        body = line.rstrip("\r\n")
        head = _HEADING.match(body) or _BOLD_HEADING.match(body)
        numbered = _NUMBERED.match(body)
        if head is not None:
            finish(offset)
            heading = _plain(head.group(1)) or "numbered list"
            list_indent = None
        elif numbered is not None:
            indent = len(body.expandtabs(4)) - len(body.expandtabs(4).lstrip(" "))
            if active is not None and indent > active["indent"]:
                pass  # nested item: retain it as detail inside the enclosing top-level item
            elif list_indent is not None and indent > list_indent:
                pass
            else:
                finish(offset)
                if list_indent is None:
                    list_indent = indent
                active = {
                    "start": offset, "line_end": offset + len(line), "indent": indent,
                    "ordinal": int(numbered.group(1)), "collection": heading,
                }
        offset += len(line)
    finish(len(source))
    return tuple(anchors)


_EXECUTION_EVIDENCE_KINDS = frozenset({
    "execution_receipt", "execution_receipt_aggregate", "execution_receipt_coverage",
    "execution_receipt_absence",
})
_QUALITY_EVIDENCE_KINDS = frozenset({"quality_exchange", "quality_exchange_coverage"})


def _execution_projection_signature(referents: Iterable[Mapping]) -> dict:
    aggregate = next((item for item in referents
                      if item.get("kind") == "execution_receipt_aggregate"), None)
    coverage = next((item for item in referents
                     if item.get("kind") == "execution_receipt_coverage"), None)
    absence = next((item for item in referents
                    if item.get("kind") == "execution_receipt_absence"), None)
    return {
        "state": "aggregate" if aggregate is not None else "absence" if absence is not None else "missing",
        "projection_sha256": str((aggregate or {}).get("projection_sha256") or ""),
        "source_set_sha256": str((aggregate or {}).get("source_set_sha256") or ""),
        "receipt_count": int((aggregate or {}).get("receipt_count", 0) or 0),
        "candidate_set_sha256": str((coverage or {}).get("candidate_set_sha256") or ""),
        "missing_set_sha256": str((coverage or {}).get("missing_set_sha256") or ""),
        "corrupt_set_sha256": str((coverage or {}).get("corrupt_set_sha256") or ""),
        "corrupt_artifact_count": int((coverage or {}).get("corrupt_artifact_count", 0) or 0),
        "ambiguous_order_set_sha256": str(
            (coverage or {}).get("ambiguous_order_set_sha256") or ""
        ),
        "coverage": str((coverage or {}).get("coverage") or "unavailable"),
    }


def _quality_projection_signature(projections: Iterable[Mapping]) -> dict:
    coverage = next((item for item in projections
                     if item.get("kind") == "quality_exchange_coverage"), None)
    return {
        "source_set_sha256": str((coverage or {}).get("source_set_sha256") or ""),
        "candidate_set_sha256": str((coverage or {}).get("candidate_set_sha256") or ""),
        "candidate_turn_artifacts": int((coverage or {}).get("candidate_turn_artifacts", 0) or 0),
        "complete_exchange_pairs": int((coverage or {}).get("complete_exchange_pairs", 0) or 0),
        "missing_exchange_count": int((coverage or {}).get("missing_exchange_count", 0) or 0),
        "partial_response_pairs": int((coverage or {}).get("partial_response_pairs", 0) or 0),
        "grounding_artifact_count": int((coverage or {}).get("grounding_artifact_count", 0) or 0),
        "missing_grounding_artifact_count": int(
            (coverage or {}).get("missing_grounding_artifact_count", 0) or 0
        ),
        "corrupt_artifact_count": int((coverage or {}).get("corrupt_artifact_count", 0) or 0),
        "corrupt_set_sha256": str((coverage or {}).get("corrupt_set_sha256") or ""),
        "ambiguous_order_set_sha256": str(
            (coverage or {}).get("ambiguous_order_set_sha256") or ""
        ),
        "grounding_set_sha256": str((coverage or {}).get("grounding_set_sha256") or ""),
        "coverage": str((coverage or {}).get("coverage") or "unavailable"),
    }


def make_evidence_snapshot(
    admission, projections: Iterable[Mapping], source_turn_id: str, *,
    snapshot_basis: Mapping | None = None, source_generation: int | None = None,
) -> dict | None:
    """Freeze source identities and digests—not payload bytes—for an adjacent verification turn."""
    execution_query = getattr(admission, "evidence_query", None)
    quality_query = getattr(admission, "quality_evidence_query", None)
    if (execution_query is None and quality_query is None) or not isinstance(snapshot_basis, Mapping):
        return None
    execution_refs = tuple(
        dict(ref) for ref in (getattr(admission, "referents", ()) or ())
        if isinstance(ref, Mapping) and str(ref.get("kind") or "") in _EXECUTION_EVIDENCE_KINDS
    )
    quality_rows = tuple(
        dict(item) for item in (projections or ())
        if isinstance(item, Mapping) and str(item.get("kind") or "") in _QUALITY_EVIDENCE_KINDS
    )
    basis = dict(snapshot_basis)
    if execution_query is not None \
            and basis.get("execution_signature") != _execution_projection_signature(execution_refs):
        return None
    if quality_query is not None \
            and basis.get("quality_signature") != _quality_projection_signature(quality_rows):
        return None
    return json.loads(json.dumps({
        "v": 2,
        "source_turn_id": str(source_turn_id or ""),
        "source_generation": int(source_generation or 0),
        "execution_query": execution_query.to_dict() if execution_query is not None else None,
        "quality_query": quality_query.to_dict() if quality_query is not None else None,
        "basis": basis,
    }, ensure_ascii=False))


__all__ = [
    "AdmissionPreview",
    "DiscourseAnchor",
    "extract_addressable_anchors",
    "extract_pending_proposal",
    "make_evidence_snapshot",
]
