"""Artifact-handle classification and legacy evidence-metadata normalization.

Since 0.3 the runtime returns complete child reports directly and never projects a fan-in
bundle. These helpers are the live slivers: they classify canonical artifact handles for
read effects (``tools.py``) and bound legacy/artifact evidence metadata folded into the
call ledger by the live reducer (``slice_reducer.py``). They own no mutable state and
therefore cannot perturb WorkGraph CAS revisions.
"""
from __future__ import annotations

from collections.abc import Mapping
import re


MAX_ACCOUNT_PATHS = 16
MAX_FIELD_CHARS = 300

_ARTIFACT_HANDLE = re.compile(r"^(?:\./)?(?:artifacts|subagents)/([^/]+)\.md$")
_ARTIFACT_EVIDENCE_HANDLE = re.compile(
    r"^(?:\./)?artifacts/([^/]+)/evidence/(?:index|obs-\d+-page-\d+)\.md$"
)
_CONTEXT_CHILD_HANDLE = re.compile(
    r"^@sliceagent/evidence/children/([^/]+)(?:\.md|/evidence/(?:index|obs-\d+-page-\d+)\.md)$"
)
_PARTIAL_MARKERS = (
    "<system>read_file ",
    "[truncated",
    "[…",
    " paged out ",
)


def _bounded_text(value: object, *, limit: int = MAX_FIELD_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def canonical_artifact_id(resource_kind: object, handle: object) -> str:
    """Return the root sealed child/artifact id for a canonical report or evidence page."""
    kind = str(getattr(resource_kind, "value", resource_kind) or "").casefold()
    if kind not in {"artifact", "subagent", "internal_context"}:
        return ""
    normalized = str(handle or "").strip().replace("\\", "/")
    patterns = (
        (_CONTEXT_CHILD_HANDLE,) if kind == "internal_context"
        else (_ARTIFACT_HANDLE, _ARTIFACT_EVIDENCE_HANDLE)
    )
    match = next((pattern.fullmatch(normalized) for pattern in patterns
                  if pattern.fullmatch(normalized)), None)
    return _bounded_text(match.group(1), limit=200) if match else ""


def artifact_view_kind(resource_kind: object, handle: object) -> str:
    """Classify a canonical artifact resource without conflating evidence pages with report consumption."""
    kind = str(getattr(resource_kind, "value", resource_kind) or "").casefold()
    normalized = str(handle or "").strip().replace("\\", "/")
    if not canonical_artifact_id(kind, normalized):
        return ""
    if _ARTIFACT_EVIDENCE_HANDLE.fullmatch(normalized) \
            or (kind == "internal_context" and "/evidence/" in normalized):
        return "evidence"
    return "report"


def artifact_read_coverage(
    args: object, text: object, *, resource_kind: object = "", handle: object = "",
) -> str:
    """Conservatively prove a complete origin-to-end artifact read.

    Exact virtual artifact documents are returned atomically by their provider, so coverage comes from that
    typed route rather than scanning report prose for words such as "truncated" or "paged out". Generic/legacy
    callers retain the conservative text-marker fallback.
    """
    output = str(text or "")
    lowered = output.casefold()
    if not output:
        return "partial"
    first = lowered.splitlines()[0] if lowered.splitlines() else ""
    if (first.startswith(("artifacts/", "subagents/", "@sliceagent/"))
            and any(marker in first for marker in (": no such ", ": not an ", ": not a "))):
        return "partial"
    values = args if isinstance(args, Mapping) else {}
    if values.get("offset") is not None or values.get("limit") is not None:
        return "partial"
    if canonical_artifact_id(resource_kind, handle):
        return "complete"
    if any(marker in lowered for marker in _PARTIAL_MARKERS):
        return "partial"
    return "complete"


def normalize_evidence_status(value: object) -> str:
    raw = _bounded_text(value, limit=40).casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "": "not_assessed",
        "none": "none",
        "unknown": "not_assessed",
        "unassessed": "not_assessed",
        "not_assessed": "not_assessed",
        "locator": "locator_only",
        "locator_only": "locator_only",
        "navigation": "navigation_only",
        "navigation_only": "navigation_only",
        "partial": "content_partial",
        "source_partial": "content_partial",
        "content_partial": "content_partial",
        "assessed": "content_retained",
        "complete": "content_retained",
        "source_complete": "content_retained",
        "content_retained": "content_retained",
        "unsupported": "unsupported",
        "source_unsupported": "unsupported",
    }
    return aliases.get(raw, "not_assessed")


def normalize_evidence_account(value: object) -> dict[str, object]:
    """Bound optional provider metadata without depending on one evolving wire shape."""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, object] = {}
    try:
        if "v" in value and not isinstance(value.get("v"), bool):
            out["v"] = max(1, min(int(value.get("v") or 1), 100))
    except (TypeError, ValueError, OverflowError):
        pass
    status = normalize_evidence_status(value.get("status"))
    if "status" in value:
        out["status"] = status
    for key in (
        "scope_path_count", "navigation_success_count", "content_success_count",
        "gap_observation_count", "retained_navigation_view_count",
        "retained_content_view_count", "omitted_navigation_view_count",
        "omitted_content_view_count", "truncated_content_view_count",
    ):
        try:
            if key in value and not isinstance(value.get(key), bool):
                out[key] = max(0, min(int(value.get(key) or 0), 10_000))
        except (TypeError, ValueError, OverflowError):
            pass
    for key in ("scope_paths", "navigation_paths", "content_paths", "gap_paths"):
        raw = value.get(key)
        if isinstance(raw, (list, tuple)):
            out[key] = tuple(
                item for item in (
                    _bounded_text(row, limit=400) for row in raw[:MAX_ACCOUNT_PATHS]
                ) if item
            )
    # Tolerate the pre-contract/generic shapes used by third-party providers.
    for key in ("observations", "claims", "files", "sources", "gaps"):
        raw = value.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            out[key] = max(0, min(raw, 10_000))
        elif isinstance(raw, (list, tuple)):
            out[key] = tuple(
                item for item in (
                    _bounded_text(row) for row in raw[:MAX_ACCOUNT_PATHS]
                ) if item
            )
    for key in ("observation_count", "claim_count", "file_count", "source_count", "gap_count"):
        try:
            if key in value and not isinstance(value.get(key), bool):
                out[key] = max(0, min(int(value.get(key) or 0), 10_000))
        except (TypeError, ValueError, OverflowError):
            pass
    if isinstance(value.get("report_required"), bool):
        out["report_required"] = value["report_required"]
    return out


__all__ = [
    "artifact_read_coverage", "artifact_view_kind", "canonical_artifact_id",
    "normalize_evidence_account", "normalize_evidence_status",
]
