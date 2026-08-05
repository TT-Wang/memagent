"""Dependency-first projection of Active Work into provider context.

The legacy region renderer still owns individual physical views during migration.  This compiler decides
*which semantic material is relevant* before the elasticity controller decides how faithfully to represent
it.  When no Active Work graph exists it returns the legacy blocks unchanged, keeping old checkpoints and
small embedding hosts compatible without creating a second admission heuristic.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from .active_work import SourceMismatchError, WorkGraph, WorkItem
from .context import (
    ContextBlock,
    EpistemicRole,
    Fidelity,
    FreshnessClass,
    InstructionClass,
    RepresentationLoss,
    SourceRef as ContextSourceRef,
)
from .receipts import receipt_summary_parts


# These are host-owned live control surfaces, not optional topical furniture. Child outcomes are ordinary
# current-turn tool results; they deliberately do not become a second cross-turn context region.
# session_spine: frozen sealed-turn bytes are the session's durable record and must reach BOTH lanes
# through this one seam (SESSION-SPINE-ROADMAP P4); the region self-suppresses when the flag is off
# or the spine is empty, so unconditional selection costs nothing outside the spine layout.
_ALWAYS = frozenset({"focus", "reconciliation", "session_spine", "session_tape"})
_INTENT_FALLBACK = frozenset({"intent", "task_objective", "corrections", "task_constraints"})
_FILE_KINDS = frozenset({"file", "workspace_file", "path", "workspace", "git"})
def _region_name(block: ContextBlock) -> str:
    prefix = "region:"
    item = block.item_id
    return item[len(prefix):] if item.startswith(prefix) else item


def dependency_resource_paths(graph: WorkGraph, *, workspace_epoch: int | None = None) -> tuple[str, ...]:
    """Workspace paths named by the unresolved dependency closure, stable and deduplicated."""
    paths = []
    for item in graph.dependency_closure():
        for ref in item.resource_refs:
            if workspace_epoch is not None and ref.workspace_epoch != workspace_epoch:
                continue
            if ref.kind in _FILE_KINDS and ref.ref not in {"workspace", "*", "."}:
                paths.append(ref.ref)
    return tuple(dict.fromkeys(paths))


# Rendered when a work item's exact user source cannot be recovered (SourceMismatch / missing event).
# Named so observers (records.AdmissionMetrics) can count missing-source renders without prose guessing —
# this exact machine-generated literal is the ONLY sound string to match on.
SOURCE_UNAVAILABLE_MARKER = "exact source: UNAVAILABLE"


def _extract_source(item: WorkItem, sources: Mapping[str, str]) -> tuple[str, ...]:
    out = []
    for ref in item.source_refs:
        text = sources.get(ref.event_id)
        if text is None:
            raise SourceMismatchError(f"source event {ref.event_id!r} is unavailable")
        out.append(ref.extract(text))
    return tuple(out)


def _render_item(
    item: WorkItem, *, sources: Mapping[str, str], current_logical_id: str,
    source_locator_prefix: str = "",
) -> str:
    mark = {
        "open": " ", "in_progress": "~", "waiting_user": "?", "waiting_peer": "⇄", "ready": "•", "delivered": "x",
        "verified": "✓", "cancelled": "-", "superseded": "→",
    }.get(item.status, " ")
    lines = [f"- [{mark}] {item.id} · {item.kind} · {item.status}"]
    if item.kind == "request":
        if item.logical_id == current_logical_id:
            lines.append("  ownership: HOST-OWNED CURRENT REQUEST ROOT — never pass this ID to update_work")
            lines.append("  exact source: CURRENT REQUEST below (shown once)")
        else:
            try:
                exact = _extract_source(item, sources)
            except SourceMismatchError:
                lines.append(f"  {SOURCE_UNAVAILABLE_MARKER} — use the immutable event locator below; do not guess")
            else:
                for text in exact:
                    lines.extend(("  user source (verbatim): |", *(f"    {line}" for line in text.splitlines())))
    elif item.description:
        # Model-maintained work state is useful control state but never promoted to user authority.
        lines.append(f"  model-maintained description: {item.description}")
    lines.append("  source event(s): " + ", ".join(ref.event_id for ref in item.source_refs))
    if source_locator_prefix:
        prefix = source_locator_prefix.rstrip("/")
        lines.append("  source locator(s): " + ", ".join(
            f"{prefix}/{ref.event_id}.md" for ref in item.source_refs
        ))
    if item.dependencies:
        lines.append("  depends on: " + ", ".join(item.dependencies))
    if getattr(item, "done_when", ""):
        lines.append(f"  done when: {item.done_when}")
    if getattr(item, "verify", ()):
        lines.append("  verify: " + " && ".join(item.verify))
    if item.resource_refs:
        lines.append("  resources: " + ", ".join(
            f"{ref.kind}:{ref.ref}@workspace-{ref.workspace_epoch}"
            + (f"#{ref.revision}" if ref.revision else "") for ref in item.resource_refs
        ))
    if item.evidence_refs:
        lines.append("  evidence: " + ", ".join(
            f"{ref.kind}:{ref.ref}" + (f" [{ref.qualifier.replace('_', ' ')}]" if ref.qualifier else "")
            for ref in item.evidence_refs
        ))
    if item.output_refs:
        lines.append("  delivered outputs: " + ", ".join(f"{ref.kind}:{ref.ref}" for ref in item.output_refs))
    if item.superseded_by:
        lines.append(f"  superseded by: {item.superseded_by}")
    return "\n".join(lines)


def render_active_work(
    graph: WorkGraph,
    sources: Mapping[str, str] | None = None,
    *,
    current_logical_id: str = "",
    source_locator_prefix: str = "",
) -> str:
    """Render unresolved work plus its dependency/ownership closure without rewriting user language."""
    if not graph.items:
        return ""
    sources = sources or {}
    closure = graph.dependency_closure()
    if not closure:
        return ""
    body = "\n".join(
        _render_item(
            item, sources=sources, current_logical_id=current_logical_id,
            source_locator_prefix=source_locator_prefix,
        )
        for item in closure
    )
    return (
        "# ACTIVE WORK (the semantic frontier; exact user source outranks model-maintained descriptions)\n"
        f"graph revision: {graph.revision}\n{body}\n\n"
    )


def _quoted(value: object) -> str:
    """Keep every prior-exchange line visible as quoted data, including blank lines."""
    return "\n".join("> " + line for line in str(value or "").split("\n"))


# The last N COMPLETED exchanges kept resident verbatim. This is a bounded CONSTANT, not a transcript: it is
# O(1) in session length (older turns page to history/ and recall by address), so it does not reintroduce the
# accumulation the slice exists to prevent. One antecedent resolves a bare "yes"; three cover the real reach of
# deictic intent ("combine the last two", "like the fetch function") without a relevance-recall round-trip
# (which fires ~0 on coding turns, so a too-tight window silently mis-resolves rather than recalling).
_ADJACENCY_ROUNDS = 3


# (_adjacency_blocks retired in wave 2 with the conversation machinery — asks live in
# tape digests, replies as frozen [reply] entries; ring trimming stays in pfc.py.)


def _receipt_block(s, *, order: int = 2) -> ContextBlock | None:
    receipt = getattr(getattr(s, "continuity", None), "last_receipt", None)
    if not isinstance(receipt, Mapping):
        return None
    artifact_id = str(getattr(s.continuity, "last_receipt_artifact_id", "") or "")
    parts = receipt_summary_parts(receipt)
    lines = [
        "# LATEST SEALED EXECUTION RECEIPT (lifecycle arithmetic; not proof of "
        "correctness, world state, or task satisfaction)",
        f"disposition: {receipt.get('disposition') or 'unknown'}",
        *(f"- {part}" for part in parts),
    ]
    warning_count = int(receipt.get("warning_count") or 0)
    if warning_count:
        lines.append(f"- {warning_count} warning(s); open the artifact for exact detail")
    recovery_artifact_id = str(
        getattr(s.continuity, "recovery_child_artifact_id", "") or ""
    )
    recovery_report_count = max(0, int(
        getattr(s.continuity, "recovery_child_report_count", 0) or 0
    ))
    if recovery_artifact_id and recovery_report_count:
        noun = "report" if recovery_report_count == 1 else "reports"
        verb = "is" if recovery_report_count == 1 else "are"
        lines.append(
            f"- crash recovery: {recovery_report_count} returned child {noun} {verb} retained in the "
            "interrupted turn's journal, but process death may have preceded parent synthesis"
        )
        lines.append(
            f'- if the current request resumes that work, read_file("artifacts/{recovery_artifact_id}.md") '
            "before synthesizing or claiming completion; use the journaled tool-outcome text, not receipt "
            "counts, as the report evidence"
        )
    if artifact_id:
        lines.append(f'- exact receipt: read_file("artifacts/{artifact_id}.md")')
    return ContextBlock(
        block_id="active-receipt:full", item_id="active-receipt",
        alternative_group="active-receipt", priority=94,
        instruction_class=InstructionClass.DATA,
        freshness=FreshnessClass.REVISION_BOUND, fidelity=Fidelity.FULL,
        representation_loss=RepresentationLoss.NONE, content="\n".join(lines) + "\n\n",
        order=order, slot=5, epistemic_role=EpistemicRole.OBSERVATION,
        scope=("task", "latest_segment"),
        source_refs=(ContextSourceRef("artifact", artifact_id or "latest-sealed-receipt"),),
    )
def compile_active_context(
    s,
    legacy_blocks: Iterable[ContextBlock],
    *,
    source_texts: Mapping[str, str] | None = None,
    current_logical_id: str = "",
    workspace_epoch: int | None = None,
) -> tuple[ContextBlock, ...]:
    """Select semantically required blocks, then hand alternatives to elasticity.

    There is intentionally no lexical intent classifier here. Relevance comes from the unresolved graph
    closure and typed source/resource/evidence references; the one exception is an already-admitted bounded
    L2 knowledge block whose backend has independently hard-scoped and relevance-ranked its records.
    """
    blocks = tuple(legacy_blocks)
    graph = getattr(s, "active_work", None)
    if not isinstance(graph, WorkGraph) or not graph.items:
        return blocks

    sources = source_texts or {}
    active_text = render_active_work(graph, sources, current_logical_id=current_logical_id)
    if not active_text:
        return blocks
    closure = graph.dependency_closure()
    resource_kinds = {
        ref.kind for item in closure for ref in item.resource_refs
        if workspace_epoch is None or ref.workspace_epoch == workspace_epoch
    }
    has_evidence = any(item.evidence_refs for item in closure)
    missing_prior_source = any(
        item.kind == "request" and item.logical_id != current_logical_id
        and any(ref.event_id not in sources for ref in item.source_refs)
        for item in closure
    )

    selected = set(_ALWAYS)
    if any(_region_name(block) == "memory" for block in blocks):
        selected.add("memory")
    if resource_kinds & _FILE_KINDS:
        selected.update(("open_files", "worktree", "related_code"))
    if "skill" in resource_kinds or getattr(s, "active_skills", None):
        selected.add("skills")
    if resource_kinds & {"memory", "history"}:
        selected.update(("memory",))
    if has_evidence:
        selected.add("findings")
    if missing_prior_source:
        # Recovery fallback only.  A healthy ledger uses Active Work as the sole semantic owner.
        selected.update(_INTENT_FALLBACK)

    kept = [block for block in blocks if _region_name(block) in selected]
    # Under the spine layout the live work graph is per-turn content and must render BELOW the
    # frozen spine (slot 2, beside the intent family it replaces); legacy keeps the head slot.
    _aw_slot = 2   # below the tape, beside the intent family (wave 2: unconditional)
    kept.append(ContextBlock(
        block_id="active-work:full", item_id="active-work", alternative_group="active-work",
        priority=100, instruction_class=InstructionClass.USER,
        freshness=FreshnessClass.REVISION_BOUND, fidelity=Fidelity.FULL,
        representation_loss=RepresentationLoss.NONE, content=active_text,
        mandatory=True, order=-1, slot=_aw_slot, epistemic_role=EpistemicRole.DIRECTIVE,
        scope=("task",),
        source_refs=tuple(ContextSourceRef("user_utterance", ref.event_id)
                          for item in closure for ref in item.source_refs),
    ))
    receipt = _receipt_block(s)
    if receipt is not None:
        kept.append(receipt)
    return tuple(sorted(kept, key=lambda block: (block.order, block.block_id)))


__all__ = [
    "compile_active_context", "dependency_resource_paths", "render_active_work",
]
