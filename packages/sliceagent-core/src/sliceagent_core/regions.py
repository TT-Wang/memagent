"""Typed-region renderers — per-kind views over the EXISTING Slice dataclass fields.

The slice is an address space of TYPED REGIONS (open files, ghosts, conversation, skills,
threads, …); each region knows how to render itself and to SUPPRESS itself when empty.
seed.py's render_slice is the layout pass that orders these region renderers into the one
user string (the moat); the renderers themselves live here.

This module is a pure rendering/metadata layer: it reads Slice fields (pfc.py) and low-level
helpers (safety.wrap_untrusted, the working-set bounds OWNED by swap.py) but imports NOTHING
from pfc.py/seed.py — they import FROM here (one direction), so there is no import cycle.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .context import (ContextBlock, ContextSelection, ElasticityController, EpistemicRole,
                      Fidelity, FreshnessClass, InstructionClass, RepresentationLoss,
                      ResourceKind, ResourceRef, SourceRef, reserved_resource_ref)
from .safety import wrap_untrusted
from .text_utils import normalize_ws, one_line
from .tool_identity import canonical_tool_args

MANIFEST_TURNS = 50      # PAGED-OUT HISTORY manifest window — bounded locator count (the moat: constant
# size regardless of session length; content is paged in on demand, never accumulated into the slice).
MAX_OPEN_THREADS = 6  # OTHER OPEN THREADS tier cap — bounded presentation of parked topics
MAX_FINDINGS = 8         # legacy compact-render default; the elastic SeedPlan projects the full relevant set
MAX_FINDING_CHARS = 300  # each finding is ONE compact line — distilled, never narration (causal tail matters)
MAX_PLAN_ITEMS = 20      # bounded PLAN (TodoWrite) — same no-unbounded-growth rule as requirements
MAX_PLAN_CHARS = 300     # each plan step is ONE compact line (multi-file scope must survive)
_PLAN_MARK = {"done": "x", "in_progress": "~", "pending": " "}

MAX_REPORT_CHARS = 280   # OPEN USER REPORT — one compact verbatim line (bounded; never a transcript)
MAX_ACTION_LOG = 24      # bounded anti-loop tally (no-transcript: the action_log can't grow per-topic forever)

# Working-set view caps (the OPEN FILES region). A working-set file is shown IN FULL up to
# FULL_FILE_LINES; only a PATHOLOGICALLY huge file collapses to its RELEVANT REGION (REGION_LINES).
# Co-located here because they parameterize the OPEN FILES region renderer (build_artifacts in
# seed.py imports them from here — one direction). DISCOVERY_K is the RELATED CODE region's k.
FULL_FILE_LINES = 1200
REGION_LINES = 400
DISCOVERY_K = 6
MAX_CONVERSATION = 4     # RECENT CONVERSATION ring — the FLOOR: the last N completed user<->assistant exchanges,
# kept VERBATIM (no per-message truncation). The bound is a COUNT floor + the token-budget reserve below, never a
# per-message byte cap: the last few turns are the active loop's antecedents ("go with your recommendation" /
# "save this") and must survive intact so a deictic follow-up resolves against them directly instead of falling to
# relevance-recall. Peak flexes with recent reply size but stays bounded across SESSION LENGTH (older turns page
# to @sliceagent/history/ and recall on demand).
# (render_conversation drops the in-progress turn, so this surfaces the last MAX_CONVERSATION-1 completed turns.)

# ── VERBATIM USER RESERVE (Codex-parity, adapted) ────────────────────────────
# Codex CLI exempts the newest ~20k tokens of user messages from all compaction. The sliceagent
# adaptation reserves PAIRED EXCHANGES (user verbatim + its response — adjacency semantics need the
# pair) under a token budget, SOFT: reserved rows carry RESERVE_PRIORITY so they degrade only as the
# true last resort, never becoming hard-unfit (a hard exemption inflates the ContextUnfitError floor
# on small windows — measured risk, see the convergence spec P0.3). The budget prices the WHOLE pair
# (user + assistant chars), so the widened ring can never inflate the per-turn peak by more than the
# reserve constant: the bound stays a constant, which is the moat invariant. This closes the
# mid-distance window: turns 5..~12 whose request roots completed used to leave the prompt entirely.
USER_RESERVE_TOKENS = 20_000   # ONE knob; chars via execution.tokens_to_chars (shared _TOKENS_PER_BYTE)
RESERVE_ROWS_CEILING = 12      # hard O(1) cap on the widened ring/adjacency, independent of budget
RESERVE_PRIORITY = 98          # BASE of the reserved band: above every ordinary DEGRADABLE region
# (highest non-mandatory region priority is 97; mandatory regions are never degradation candidates).
# Reserved adjacency pairs ascend from this base with RECENCY (base + reserved-1 - age): a FLAT band
# would let tie-breaking fall to savings, degrading the LARGEST reserved pair first and inverting the
# oldest-pages-first invariant the adjacency tests pin. Still soft: every reserved block keeps its
# locator alternative, so ContextUnfit semantics are preserved.


def user_reserve_chars() -> int:
    from .execution import tokens_to_chars   # lazy: regions stays a rendering/metadata layer
    return tokens_to_chars(USER_RESERVE_TOKENS)


def reserve_keep(rows, *, floor: int, ceiling: int = RESERVE_ROWS_CEILING) -> int:
    """How many NEWEST conversation rows to keep/reserve.

    Walks newest-first accumulating len(user)+len(assistant) chars. Always keeps `floor` rows
    (legacy count bound — giant messages inside the floor are kept regardless, they degrade via
    the elasticity path instead); beyond the floor, extends only while the cumulative chars fit
    the reserve budget; hard-capped at `ceiling` so the ring stays O(1). floor=0 answers "how
    many newest rows are RESERVED" (within budget) for priority marking."""
    budget = user_reserve_chars()
    keep = used = 0
    for row in reversed(tuple(rows)):
        if keep >= ceiling:
            break
        row_chars = len(str(row.get("user") or "")) + len(str(row.get("assistant") or ""))
        if keep >= floor and used + row_chars > budget:
            break
        keep += 1
        used += row_chars
    return keep


# ── PER-REGION RENDER: UNCAPPED-BY-RELEVANCE ──────────────────────────────────
# _NO_CAP — the "no render cap" sentinel. OPEN FILES / YOUR NOTES are bounded by RELEVANCE
# (record_note dedup/retire), never by an arbitrary size cap — bound ≠ size, the slice shows all that's
# relevant. The only hard limit is the physical context window, handled by the loop's overflow path
# (drop the oldest accumulated exchange), not by truncating a tier.
_NO_CAP = 1_000_000


# `one_line` is re-exported from text_utils (single definition — pfc.py/seed.py/neocortex.py import the
# real definition directly). Kept importable from regions too for the existing call sites here.


def render_cache_manifest(refs) -> str:
    """PAGED-OUT HISTORY body: one locator line per earlier turn of THIS session (NOT in the slice),
    each ending with the EXACT read_file call to page it back — so reaching back is copy-paste, not a
    blind guess. This is the TRIGGER the dead recall channel was missing: a cache the model can't see is
    a cache it never calls (the read-side analogue of REPO MAP advertising file paths). The turns are
    read-only virtual files under @sliceagent/history/ — the model reaches for read_file far more readily than a
    bespoke recall tool (measured 2026-07-06). ``refs`` are locator-only PageRefs from
    PageTable._episodes_thissession (ONE read seam); this is pure formatting. MOAT: locators only —
    turn/title/breadcrumb, never content; the turn's body is served on demand from the bounded seal."""
    if not refs:
        return ""
    lines = []
    for r in refs:
        if r.handle == "…older":
            preview = str(r.preview).replace(
                'read_file("history/index.md")', 'read_file("@sliceagent/history/index.md")',
            )
            lines.append(f"- {preview}")            # the "+N earlier" tail (no single-turn call)
        else:
            lines.append(f'- {r.preview}  → read_file("@sliceagent/history/turn-{r.handle}.md")')
    return "\n".join(lines)


def render_focus(focus, extra_roots, *, home: str = "", workspace: str = "") -> str:
    """CURRENT PROJECT body: the dir the agent is actively working in, when it has moved beyond the primary
    root. Surfaces the grounded ReachSet + the moved relative-path base (otherwise INVISIBLE →
    the model stays in the start-dir frame and can't resolve 'the project' / a bare filename to where the
    work actually is, then re-asks or cold-searches — the hunter 'index.ts' miss). The primary workspace stays
    the project identity until change_workspace; focus is its task-local frame. Self-suppresses for one project."""
    def short(p: str) -> str:
        return ("~" + p[len(home):]) if home and p.startswith(home) else p
    roots = [r for r in (extra_roots or []) if r and r != workspace]
    if not roots and not (focus and focus != workspace):
        return ""
    lines = []
    if focus and focus != workspace:
        lines.append(
            f"You are now working in `{short(focus)}`. Bare relative paths resolve HERE, and your file "
            f"tools — read_file, list_files, grep, edit_file — act here. Resolve a bare filename or "
            f"\"the project\"/\"it\" against THIS and the RECENT CONVERSATION first; do NOT fall back to a "
            f"broad search or re-ask when the referent is already clear from recent work.")
    others = [short(r) for r in roots if r != focus]
    if others:
        lines.append("Also in the grounded ReachSet (reachable by file tools): " + ", ".join(f"`{o}`" for o in others) + ".")
    return "\n".join(lines)


def render_skills(active_skills: list[dict]) -> str:
    if not active_skills:
        return wrap_untrusted("", kind="skill")
    joined = "\n\n".join(f"## SKILL: {sk['name']}\n{sk['body']}" for sk in active_skills)
    return wrap_untrusted(joined, kind="skill")


def render_threads(refs) -> str:
    """Render the bounded OTHER OPEN THREADS index (parked topics the model can resume)."""
    if not refs:
        return ""
    lines = [f"- [{r.task_id}] {r.title} ({r.status})" for r in refs[:MAX_OPEN_THREADS]]
    extra = len(refs) - min(len(refs), MAX_OPEN_THREADS)
    if extra > 0:
        lines.append(f"- …and {extra} more")
    return "\n".join(lines)


def render_conversation(s) -> str:
    """The RECENT CONVERSATION tier: the last few COMPLETED user<->assistant exchanges (the in-progress
    one is excluded — its user message is the current task). Ends with a pointer to recall the rest."""
    prior = [e for e in s.conversation[:-1] if e.get("user")]
    if not prior:
        return ""
    lines = []
    for e in prior:
        lines.extend(("--- recent exchange ---", "user (verbatim):", str(e["user"])))
        if e.get("assistant"):
            lines.extend(("assistant (verbatim):", str(e["assistant"])))
        lines.append("--- end recent exchange ---")
    older = s.turns - len(prior) - 1  # turns beyond the ring (minus the current in-progress turn)
    tail = (f"\n(+{older} earlier turn(s) this session not shown — they're listed in PAGED-OUT HISTORY "
            "below; read_file(\"@sliceagent/history/turn-N.md\") to view any)") if older > 0 else ""
    return "\n".join(lines) + tail


# I1 PROVENANCE — narration filter. A FINDING must be a durable FACT, never the model's running
# narration. Notes that merely announce intent ("Let me run it", "I'll check the file", "Now I'll
# edit X", "Next, …") carry no established fact: folding them made FINDINGS read like a transcript
# and let "**Done — built it**" ratchet as an ESTABLISHED truth (F1/C3/G5). Task-agnostic + cheap:
# pure lexical, no LLM. Matched at the START of the note (the leading clause sets its kind).
_NARRATION_RE = re.compile(
    r"^\s*(?:ok(?:ay)?[,. ]+)?(?:"
    r"(?:let'?s|let me|let us|i['’]?ll|i will|i['’]?m going to|i am going to|now i|now let|"
    r"then i|i need to|i should|going to|gonna|i plan to)\b"
    r"|(?:next|first|then)\b[,. ]"  # leading sequencing adverbs ("Next, …", "First …")
    r")",
    re.I,
)
# A note that ASSERTS completion ("done", "all set", "task complete", "finished") is a CLAIM, not an
# observation — durable ONLY if a real tool RESULT backed it (see slice_sink). Detected so the source
# can be DOWNGRADED to "claim" (rendered "unverified — confirm against OPEN FILES"), never silently
# promoted to an established truth. Task-agnostic lexical signal; no LLM.
_DONE_CLAIM_RE = re.compile(
    r"\b(?:done|all set|all done|complete(?:d|ly)?|finished|it works|works now|ready to use|"
    r"task (?:is )?(?:done|complete)|already (?:done|complete|built|implemented)|"
    r"successfully (?:built|created|implemented|added|completed))\b",
    re.I,
)


def is_done_claim(text: str) -> bool:
    """True when `text` asserts the work is finished — a claim that needs an observation to be durable."""
    return bool(_DONE_CLAIM_RE.search(text or ""))


# RECALL-ON-CUT marker (see memory: recall-ring-truncation-gap). A silent one_line() cut with no signal
# reads as "this is the whole thing" — the model then RE-DERIVES the missing part from scratch instead of
# recalling it, and a re-derived answer usually does NOT match the original (confabulation, not correction).
# Found live TWICE via two independent cut sites (a bug-hunt reply cut in the RECENT CONVERSATION ring, then
# again cut here in FINDINGS/OPEN USER REPORT) — any NEW site that bounds model- or user-authored text with
# one_line() should go through this helper rather than a bare one_line() call.
_RECALL_ON_CUT_MARK = (' [DISPLAY PARTIAL ONLY — source/action remains intact; NOT execution failure. '
                       'Read @sliceagent/history/ or search_history("...") for omitted bytes; don\'t guess]')


def _cut_with_recall_marker(text: str, cap: int) -> str:
    """one_line(text, cap), but if the cut actually removed content, replace the tail with a marker
    naming the cut + the two general recall paths (@sliceagent/history/ and
    search_history for content across sessions) + an explicit don't-guess instruction."""
    was_cut = len(one_line(text, cap + 1)) > cap
    if not was_cut:
        return one_line(text, cap)
    return one_line(text, max(0, cap - len(_RECALL_ON_CUT_MARK))) + _RECALL_ON_CUT_MARK


def record_note(s, text: str, source: str = "tool-note") -> bool:
    """Fold the model's per-turn note (a distilled FACT it established) into the FINDINGS tier.
    Returns True iff a GENUINELY NEW finding was added (not narration, not a dedup refresh) — the
    convergence check uses this so 'actively learning' doesn't count as 'spinning' (review #5).

    The slice carries no transcript, so a reasoning model would otherwise re-derive the
    situation each turn (costly reasoning bursts). This lets it carry its OWN conclusions
    forward as task-elastic, deduplicated semantic state rather than an elapsed-turn log. Physical
    pressure is handled by the shared context controller, not destructive insertion-time truncation.

    I1 PROVENANCE: a finding is a FACT FROM THE WORLD, never raw narration. Notes that announce
    intent ("Let me…", "I'll…") are dropped — they're transcript, not established state. `source`
    tags where the fact came from ("observed", "tool-note", "delegated", or "claim"); a completion ("done")
    tool-note is downgraded to "claim", while delegated testimony keeps its explicit unverified tag. Thus
    neither can ratchet into an ESTABLISHED truth. No extra LLM call — pure lexical, captured from a real call.

    Long assistant replies do not enter this path: they remain in bounded continuity and immutable turn
    artifacts. This helper is only for explicit tool-backed notes/claims."""
    note = _cut_with_recall_marker(text, MAX_FINDING_CHARS)
    if not note:
        return False
    if _NARRATION_RE.match(note):   # pure intent/narration — carries no durable fact
        return False
    if source not in _SOURCE_TAG:
        source = "claim"  # an unknown provenance label must never render with observed-strength silence
    # a generic tool-note saying "done" is only a hypothesis. Delegated testimony is already explicitly
    # unverified and keeps its more precise provenance instead of collapsing into the generic claim bucket.
    if source == "tool-note" and is_done_claim(note):
        source = "claim"
    is_new = note not in s.findings  # genuinely new knowledge vs a refresh of an existing finding
    if not is_new:                   # already established — refresh its recency, don't duplicate
        s.findings.remove(note)
    s.findings.append(note)
    # BOUNDED = SEAL THE LOOP, not cut within it: findings are NOT truncated or retired inside a loop —
    # every distinct conclusion the loop established stays whole (any within-loop cut harms the LLM). The
    # only reduction is exact-duplicate dedup above (same fact refreshed, no information lost). The bound
    # is the loop-boundary SEAL (TurnEnd archive + a fresh next loop), never a within-section filter.
    s.finding_source[note] = source
    # keep the source map bounded to the LIVE finding set (no unbounded growth across turns)
    live = set(s.findings)
    for k in [k for k in s.finding_source if k not in live]:
        del s.finding_source[k]
    return is_new


# I1 PROVENANCE — per-source trust framing. The slice's #1 ground truth is OPEN FILES (disk);
# FINDINGS are the model's own prior notes, which must be VERIFIED, never blindly reused. We never
# render model-sourced text as "do not re-derive" (that authored the "already done" ratchet).
_SOURCE_TAG = {
    "observed": "",                          # backed by a tool result — trust, but OPEN FILES still wins
    "tool-note": " (your note — verify against OPEN FILES)",
    "delegated": (" (delegated testimony — UNVERIFIED; the successful spawn proves it was returned/sealed, "
                  "not that its workspace claims are true; check its primary observation or artifact)"),
    "claim": " (UNVERIFIED claim — confirm against OPEN FILES/a tool result before relying on it)",
}


def render_findings(findings: list[str], sources: dict | None = None) -> str:
    if not findings:
        return ""
    sources = sources or {}
    return "\n".join(
        f"- {finding}{_SOURCE_TAG.get(sources.get(finding, 'tool-note'), _SOURCE_TAG['claim'])}"
        for finding in findings
    )


def render_world(world: dict) -> str:
    """The agent's durable WORLD MODEL — a maintained key→value scratchpad (maze map, inventory,
    system state, plan). Long/multiline values render as their own block; short ones as bullets.
    No cap (bound = the seal, not a cut): the whole maintained state renders into each turn's seed."""
    if not world:
        return ""
    parts = []
    for k, v in world.items():
        v = str(v)
        if "\n" in v or len(v) > 80:
            parts.append(f"## {k}\n{v}")
        else:
            parts.append(f"- {k}: {v}")
    return "\n".join(parts)


def render_requirements(requirements: list[dict]) -> str:
    """Legacy v1 requirement rows, retained as a rendering compatibility helper."""
    if not requirements:
        return ""
    return "\n".join(f"- [{'x' if r.get('done') else ' '}] {r.get('text', '')}" + (" (done)" if r.get("done") else "")
                     for r in requirements)


def render_intent(intent, *, authorities: tuple[str, ...] | None = None,
                  kinds: tuple[str, ...] = ("constraint",)) -> str:
    """Render every resident typed obligation without an arbitrary semantic cap.

    Provisional completion stays visibly distinct from user-accepted satisfaction. Superseded/deferred
    records remain available to persistence but are not active context.
    """
    if intent is None:
        return ""
    entries = intent.resident_entries() if hasattr(intent, "resident_entries") else []
    if authorities is not None:
        entries = [entry for entry in entries if getattr(entry, "authority", "legacy") in authorities]
    entries = [entry for entry in entries if getattr(entry, "kind", "constraint") in kinds]
    lines = []
    for entry in entries:
        if entry.status == "active":
            lines.append(f"- [ ] {entry.verbatim_clause}")
        elif entry.status == "provisionally_satisfied":
            lines.append(f"- [~] {entry.verbatim_clause} (provisionally satisfied; not user-finalized)")
    return "\n".join(lines)


def render_corrections(intent) -> str:
    """Render exact newer wording without pretending every clarification is an acceptance obligation."""
    if intent is None:
        return ""
    return "\n".join(
        f"- {entry.verbatim_clause}"
        for entry in intent.resident_entries()
        if getattr(entry, "authority", "legacy") == "user"
        and getattr(entry, "kind", "constraint") == "correction"
    )


def render_turn_contract(s) -> str:
    """Render current-turn grounding and evidence needs without exposing mutation metadata as action intent."""
    intent = getattr(s, "intent", None)
    contract = getattr(intent, "turn_contract", None)
    request = str(getattr(intent, "current_request", "") or "")
    if not request.strip():
        return ""
    grounding = str(getattr(contract, "grounding", "none") or "none")
    needs = tuple(getattr(contract, "source_needs", ()) or ())
    evidence_query = getattr(contract, "evidence_query", None)
    quality_query = getattr(contract, "quality_evidence_query", None)
    delegation = getattr(contract, "delegation_requirement", None)
    modes = tuple(getattr(contract, "requested_modes", ()) or ())
    audit_mode = "audit" in modes or quality_query is not None
    source_rule = {
        "sealed_past": "answer from the sealed prior response; do not re-derive what was said from live files",
        "live_present": "answer from live workspace/tool observations",
        "both": "keep sealed prior wording and live present truth separate and label both",
        "none": "no special temporal source selected",
    }.get(grounding, "no special temporal source selected")
    if audit_mode:
        source_rule = (
            "audit past performance by keeping three sources separate: sealed user requests establish what "
            "was asked, sealed assistant responses establish what was said, and canonical receipts establish "
            "what ran; no one source can substitute for the others"
        )
    elif getattr(evidence_query, "source", None) == "execution_receipt" or "execution_receipt" in needs:
        source_rule = (
            "answer past execution from canonical recalled receipts; prior assistant wording is not "
            "execution evidence and live files cannot prove what previously ran"
        )
    lines = [f"grounding: {grounding} — {source_rule}"]
    actor = getattr(contract, "actor", None)
    target = getattr(contract, "target", None)
    if actor is not None:
        lines.append(f"actor: {getattr(actor, 'label', actor)}")
    if target is not None:
        target_source = str(getattr(target, "source", "") or "")
        suffix = f" (resolved from {target_source})" if target_source else ""
        lines.append(f"target: {getattr(target, 'label', target)}{suffix}")
    if needs:
        lines.append("authoritative source need(s): " + ", ".join(str(need) for need in needs))
    if evidence_query is not None:
        lines.append(
            "evidence query: "
            f"source={getattr(evidence_query, 'source', 'unknown')}, "
            f"family={getattr(evidence_query, 'family', 'all')}, "
            f"predicate={getattr(evidence_query, 'predicate', 'operations')}, "
            f"scope={getattr(evidence_query, 'scope', 'task')}"
        )
    if quality_query is not None:
        lines.append(
            "quality evidence query: "
            f"scope={getattr(quality_query, 'scope', 'task')}, "
            f"purpose={getattr(quality_query, 'purpose', 'assess')}, "
            f"prospective-requested={bool(getattr(quality_query, 'prospective_requested', False))}"
        )
    if delegation is not None:
        count = getattr(delegation, "count", None)
        targets = tuple(getattr(delegation, "targets", ()) or ())
        lines.append(
            "requested collaboration shape: "
            f"agent={getattr(delegation, 'agent', 'explorer')}; "
            f"exact-count={count if count is not None else 'unspecified'}; "
            f"parallel={bool(getattr(delegation, 'parallel', False))}; "
            f"targets={', '.join(targets) if targets else '(not named)'}. "
            "Honor it when available; if it cannot be completed, report the concrete limitation instead of "
            "inventing child work."
        )
    if getattr(contract, "evidence_continuation", False):
        snapshot = _evidence_snapshot(contract)
        status = str((snapshot or {}).get("status") or "unavailable")
        lines.append(
            "verification baseline: " + (
                "reuse the FROZEN prior-response evidence projection; do not count the response now being "
                "verified or reopen a newer artifact index"
                if status == "frozen" else
                "the frozen prior-response projection is unavailable; state that limitation and label any "
                "best-effort alternative source"
            )
        )
    repairs = tuple(getattr(contract, "focus_repairs", ()) or ())
    for repair in repairs:
        replacement = getattr(repair, "replacement", None)
        if replacement is not None:
            lines.append(
                f"focus repair: {getattr(repair, 'field', 'target')} → "
                f"{getattr(replacement, 'label', replacement)}"
            )
    grants = tuple(getattr(contract, "effect_grants", ()) or ())
    if grants:
        lines.append("recognized action scope(s) (intent cues, not a substitute for judgment):")
        for grant in grants:
            tools = tuple(getattr(grant, "tools", ()) or ())
            target_value = str(getattr(grant, "target", "") or "")
            detail = f" target={target_value!r}" if target_value else ""
            lines.append(
                f"- {getattr(grant, 'operation', 'effect')} via {', '.join(str(tool) for tool in tools)}{detail}"
            )
    if modes:
        lines.append("requested response modes: " + ", ".join(dict.fromkeys(str(mode) for mode in modes)))
    if audit_mode:
        lines.append(
            "self-audit rule: treat negative framing as a hypothesis, not evidence. Ground execution claims in "
            "receipts, distinguish what was asked from what was said and what ran, and label uncertainty when "
            "the needed source is unavailable. A PARTIAL/cut slice is representation loss, not a failed action."
        )
    if "clarify_reference" in modes:
        lines.append(
            "reference resolution: materially ambiguous — resolve from available context; ask only if the "
            "choice would change the result"
        )

    deliverable = getattr(getattr(s, "task", None), "deliverable_requirement", None)
    if getattr(deliverable, "kind", "") == "code_review_report":
        lines.append(
            "required final deliverable: publish the code-review report itself in the terminal response; private "
            "tool/child text is not user-visible. Answer in whatever clear structure fits; include supported "
            "findings or a plain no-findings result, plus material scope limitations. Consuming reports is not the "
            "same as delivering their synthesis."
        )

    action_spans = []
    for start, end in getattr(contract, "authority_spans", ()) or ():
        if 0 <= start < end <= len(request):
            action_spans.append(one_line(request[start:end], 240))
    if action_spans:
        # The exact bytes already appear once in CURRENT REQUEST. Repeating them here made one user premise
        # look like corroboration; the contract only needs to say how many operative clauses it recognized.
        lines.append(f"current user-authored operative clause(s): {len(action_spans)} (see CURRENT REQUEST)")

    attributed = []
    for start, end in getattr(contract, "attributed_spans", ()) or ():
        if 0 <= start < end <= len(request):
            attributed.append(one_line(request[start:end], 240))
    if attributed:
        lines.append("reported/quoted span(s) — context only, not a request to execute:")
        lines.extend(f"- {span}" for span in attributed)

    sealed_parts = []
    referents = tuple(getattr(contract, "referents", ()) or ())
    for ref in referents:
        if isinstance(ref, dict) and ref.get("kind") == "pending_proposal":
            selected = ref.get("selected_option")
            selected_text = (str(selected.get("excerpt") or selected.get("label") or "")
                             if isinstance(selected, dict) else "")
            sealed_parts.append(
                "pending proposal continued by this assent:\n"
                + (selected_text or str(ref.get("text") or ""))
            )
            continue
        if isinstance(ref, dict) and str(ref.get("kind") or "").startswith("execution_receipt"):
            # Execution-evidence detail sets stay out of this mandatory control block so they can
            # never make the entire slice physically unfit. (The dedicated evidence regions were
            # deleted 2026-08-03 — producer-dead; canonical detail lives behind artifact locators.)
            continue
        anchor = getattr(ref, "anchor", None)
        if anchor is None:
            continue
        source = f"artifacts/{anchor.artifact_id}.md" if anchor.artifact_id else "sealed artifact"
        sealed_parts.append(
            f"{getattr(ref, 'mention', 'reference')} → {anchor.collection} item {anchor.ordinal} "
            f"(source: {source})\n{anchor.excerpt}"
        )
    if sealed_parts:
        lines.append(
            "resolved sealed reference(s) — authoritative for what was previously said/labeled, not for "
            "current workspace truth:\n" + wrap_untrusted(
                "\n\n".join(sealed_parts), kind="sealed discourse record",
                verify_against_open_files=False,
            )
        )
    return "\n".join(lines)




def _evidence_snapshot(contract) -> dict | None:
    return next((
        ref for ref in (getattr(contract, "referents", ()) or ())
        if isinstance(ref, dict) and ref.get("kind") == "evidence_snapshot"
    ), None)
















def render_task_objective(s) -> str:
    """Keep the task anchor resident after the recent-conversation ring advances.

    It is the original user-authored objective, not a mutable assistant summary. The current request remains
    more recent authority and explicitly supersedes any conflicting detail.
    """
    raw_goal = str(getattr(getattr(s, "task", None), "goal", "") or "")
    goal = raw_goal.strip()
    current = str(getattr(getattr(s, "intent", None), "current_request", "") or "").strip()
    if not goal or goal == current:
        return ""
    source = str(getattr(getattr(s, "task", None), "goal_source", "") or "").strip()
    # The objective is the original request, but a clause explicitly superseded later is no longer active
    # authority. Remove only verified source ranges whose bytes still match; the archived artifact retains
    # the original wording and ACTIVE USER INTENT carries the replacement.
    spans = []
    for entry in getattr(getattr(s, "intent", None), "entries", ()):
        same_source = (not source and not entry.source_artifact) or entry.source_artifact == source
        if entry.status != "superseded" or not same_source or entry.source_range is None:
            continue
        start, end = entry.source_range
        if 0 <= start < end <= len(raw_goal) and raw_goal[start:end] == entry.verbatim_clause:
            spans.append((start, end))
    if spans:
        pieces, cursor = [], 0
        for start, end in sorted(spans):
            if start >= cursor:
                pieces.append(raw_goal[cursor:start])
                cursor = end
        pieces.append(raw_goal[cursor:])
        goal = " ".join("".join(pieces).strip(" \t\r\n;,.—-").split())
        if not goal:
            return ""
    provenance = f"\nsource artifact: {source}" if source else ""
    has_corrections = bool(render_corrections(getattr(s, "intent", None)))
    provisional = getattr(getattr(s, "task", None), "objective_status", "active") \
        == "provisionally_satisfied"
    if provisional:
        return (
            "# PRIOR TASK BACKGROUND (the original objective completed cleanly but is not user-finalized; "
            "the CURRENT REQUEST is the active instruction. Use this only for topic continuity)\n"
            f"{goal}{provenance}\n\n"
        )
    return (
        "# STABLE TASK OBJECTIVE (original user objective; keep it active across follow-ups. "
        + ("The RETAINED USER CORRECTIONS below are newer and override conflicting base details"
           if has_corrections else "A newer retained user correction supersedes any conflicting detail")
        + ")\n"
        f"{goal}{provenance}\n\n"
    )


def render_reconciliation(s) -> str:
    marker = str(getattr(s, "reconciliation_required", "") or "").strip()
    if not marker:
        return ""
    targets = tuple(getattr(s, "reconciliation_targets", ()) or ())
    scope = ", ".join(f"`{target}`" for target in targets)
    return (
        "# EXECUTION UNCERTAINTY (advisory evidence, not a permission gate)\n"
        "An earlier operation has no conclusive outcome. Do not claim that it succeeded or failed without "
        "fresh evidence. Re-observe it when relevant to the current request, and call reconcile_execution "
        "when the live result is known. Ordinary work, delegation, and task/workspace switching remain "
        "available.\n"
        + (f"possibly affected targets: {scope}\n" if scope else "")
        + f"{marker}\n\n"
    )


def render_progress_signals(signals) -> str:
    """Render semantic task state, excluding old narrative execution counters.

    ``blocked/edit/evidence`` were lossy projections of individual tool calls.  New execution receipts own
    that truth; retaining these legacy rows in old checkpoints lets unrelated turns be woven together.
    """
    if not signals:
        return ""
    semantic = [signal for signal in signals if signal.kind not in {"blocked", "edit", "evidence"}]
    return "\n".join(
        f"- {signal.kind}: {signal.detail}" + (f" (x{signal.count})" if signal.count > 1 else "")
        for signal in semantic
    )


# ── ANTI-LOOP / RECENT / CURRENT ERROR ────────────────────────────────────────
# the underlying operations inside an execute_code body — so the anti-loop tally can see
# THROUGH code-as-action (otherwise every script is a unique signature and loops hide)
_CODE_OP_RE = re.compile(
    r"\b(read_file|write_file|append_file|str_replace|list_files|run)\(\s*['\"]?([^'\",)]*)"
)


def code_ops(code: str) -> list[str]:
    """Normalized operation list inside an execute_code body (op + the tail of its literal arg)."""
    out, seen = [], set()
    for op, arg in _CODE_OP_RE.findall(code or ""):
        arg = arg.strip().split("/")[-1][:24]
        sig = f"{op} {arg}".strip()
        if sig not in seen:
            seen.add(sig)
            out.append(sig)
    return out


def observe(out, n: int = 260) -> str:
    """A one-line observation that PRESERVES THE TAIL. For most command output the decisive part —
    the verdict, the final status, the exception — is at the END, so head-only truncation hides it
    and the agent re-runs to 'see the result'. Task-agnostic: we don't interpret the outcome, we
    just guarantee the end is visible. Keep a little head for context plus the whole tail."""
    o = normalize_ws(out)
    if len(o) <= n:
        return o
    if n < 8:                            # too small to split head+sep+tail; a plain head-cut is the bound
        return o[:n]                     # (else tail = n-head-3 <= 0 and o[-0:] returns the WHOLE string)
    head = n // 4
    tail = n - head - 3                  # 3 = len(" … "); head + sep + tail == n
    return o[:head] + " … " + o[-tail:]


def action_sig(name: str, args: dict) -> str:
    if name == "run_command":
        return f"run_command `{one_line(args.get('command', ''), 50)}`"
    if name == "execute_code":
        ops = code_ops(args.get("code", ""))
        return "execute_code[" + ", ".join(ops[:4]) + "]" if ops else "execute_code(script)"
    if name in ("edit_file", "append_to_file", "str_replace", "read_file"):
        return f"{name} {args.get('path', '')}"
    if name == "list_files":
        return f"list_files {args.get('path', '.')}"
    return name


def record_action(s, name: str, args: dict, out: str, failing: bool | None = None) -> None:
    """Fold one tool result into the action tally + error/exploration state (deterministic — no LLM).

    `failing` is the AUTHORITATIVE flag from the tool layer (ToolText.ok / event.failing); the loop
    passes it. The prose heuristic is a back-compat fallback only — relying on it misclassified a grep/
    log line that legitimately starts with "Error" as a failure (corrupting last_error/anti-loop)."""
    s.turn_actions = getattr(s, "turn_actions", 0) + 1   # per-turn exploration counter (finding-independent)
    if failing is None:
        failing = out.startswith("Error") or out.startswith("Exit code")
    sig = action_sig(name, args)
    identity = name + "\0" + canonical_tool_args(args or {})
    prev = s.action_log.get(sig, {"count": 0, "failing": False})
    failure_identity = str(prev.get("failure_identity") or "")
    failure_last = str(prev.get("failure_last") or "")
    if failing:
        s.last_error = out if len(out) <= 3000 else out[:2000] + "\n…[trace truncated]…\n" + out[-900:]
        s.evidence.last_error_identity = identity
        failure_identity = identity
        failure_last = observe(out, 100)
    elif failure_identity == identity:
        # A successful retry of the same operation supersedes its stale failure. Do not clear an unrelated
        # blocker merely because some other read/edit happened to succeed.
        if s.evidence.last_error_identity == identity:
            s.last_error = ""
        failure_identity = ""
        failure_last = ""
    unresolved_failure = bool(failure_identity)
    s.action_log[sig] = {
        "count": prev["count"] + 1,
        "failing": unresolved_failure,
        "last": failure_last if unresolved_failure else observe(out, 100),
        **({"failure_identity": failure_identity, "failure_last": failure_last}
           if unresolved_failure else {}),
    }
    if len(s.action_log) > MAX_ACTION_LOG:
        # bounded like every tier (no-transcript): evict lowest-signal first — oldest one-shot,
        # non-failing entries — so failing/repeated ones (the anti-loop signal) survive longest.
        for k in [k for k, a in s.action_log.items() if a["count"] < 2 and not a["failing"]]:
            if len(s.action_log) <= MAX_ACTION_LOG:
                break
            del s.action_log[k]
        while len(s.action_log) > MAX_ACTION_LOG:
            del s.action_log[next(iter(s.action_log))]


# POSIX-general signal that a command is UNAVAILABLE (not that the agent's code is wrong): the
# shell couldn't find/execute it (exit 127 = not found, 126 = not executable). Task-agnostic — no
# tool/language/runner name. Re-running an unavailable command can never succeed.
# Deliberately NOT "no such file" (a path mistake is usually fixable, not an unavailable command).
_CMD_UNAVAILABLE = ("command not found", "[exit 127]", "exit code 127",
                    "[exit 126]", "exit code 126", "not executable", "executable not found")




# ── CONVERGENCE ───────────────────────────────────────────────────────────────
# Thresholds raised per the #33 limits review: the old 2/4-post-edit and 5/8-read pressure fired below
# the evidence needs of legitimate multi-file work ("no current error" is not verified completion) and
# caused more premature completion than the step ceiling itself. These remain SOFT checkpoints — the
# model may continue for a real reason; hard caps stay reserved for physical safety and spend.










# ── OPEN USER REPORT ──────────────────────────────────────────────────────────
# I3 — OPEN USER REPORT capture heuristic. A user follow-up that looks like a FAILURE REPORT ("it
# can't play", "it doesn't work", "still broken", "cd: no such file") is the user pushing back on a
# (possibly false) "done" — the dialectic a Markov snapshot loses. We carry it as a blocker the model
# must verify against the REAL artifact before re-claiming done (it drove the "already done" ratchet:
# F1's user-pushback half). Task-agnostic + LLM-agnostic: pure lexical, no command/tool parsing, no
# model call. Two signals: (a) negation/failure phrasing about the work, (b) a literal error/diagnostic
# pasted from a terminal (a shell/runtime error string the user is reporting back).
_USER_REPORT_RE = re.compile(
    r"(?:"
    # explicit failure/negation about the artifact
    r"\b(?:doesn'?t|does not|don'?t|do not|won'?t|will not|can'?t|cannot|can ?not)\b\s*"
    r"(?:\w+\s+){0,3}?(?:work|works|run|runs|play|plays|load|loads|open|opens|start|starts|build|builds|compile|compiles)\b"
    r"|\b(?:not|isn'?t|aren'?t|wasn'?t)\s+(?:\w+\s+){0,2}?(?:work|working|run|running|play|playing|load|loading|right|correct)\b"
    r"|\b(?:still\s+)?(?:broken|failing|fails|failed|crash(?:es|ed|ing)?|errored|buggy|not working)\b"  # bare 'error'/'bug' dropped (dev vocabulary, not a report); re-admitted with context below
    r"|\b(?:it|this|that)\s+(?:still\s+)?(?:doesn'?t|does not|won'?t|can'?t|cannot)\b"
    # a pasted terminal/runtime diagnostic the user is reporting
    r"|\b(?:no such file|command not found|traceback|exception|permission denied|"
    r"syntaxerror|nameerror|typeerror|modulenotfound|exit code|segmentation fault)\b"
    r"|:\s*no such file or directory\b"
    # phrasings the first pass missed: hangs / no-output, red|failing tests/build,
    # "didn't fix it", "same error still", HTTP 4xx/5xx in a failure context, ModuleNotFoundError.
    r"|\b(?:hang(?:s|ing|ed)?|frozen|freeze(?:s|ing)?|stuck)\b"
    r"|\bnothing (?:happen(?:s|ed)?|shows?|showed|loads?|loaded|renders?|rendered)\b"
    r"|\b(?:tests?|the build|build|ci|pipeline)\b(?:\s+\w+){0,2}?\s+(?:are|is|still|now)?\s*(?:red|failing|fail|broken)\b"
    r"|\b(?:failing|red|broken)\s+(?:tests?|build|ci)\b"
    r"|\bdid(?:n'?t|\s+not)\b(?:\s+\w+){0,2}?\s*fix\b"
    r"|\b(?:still|same)\b(?:\s+\w+){0,3}?\s+(?:error|issue|problem|bug|failure|failing|broken)\b"
    r"|\bhttp\s*[45]\d\d\b|\b[45]\d\d\s+(?:error|not found|internal server)\b"  # dropped bare 'status'/'response' (feature-spec phrasing, e.g. 'return a 404 status')
    r"|\b(?:return(?:s|ed|ing)?|get(?:s|ting)?|got|throw(?:s|n|ing)?|give[sn]?)\s+(?:an?\s+)?(?:http\s*)?[45]\d\d\b"
    r"|\bmodulenotfounderror\b"
    r")",
    re.I,
)


def is_user_report(text: str) -> bool:
    """True when a user message looks like a FAILURE REPORT about prior work — captured as an OPEN
    USER REPORT blocker. Conservative + task-agnostic (pure lexical); a normal directive that merely
    contains 'add'/'fix' is NOT a report unless it carries an explicit failure/negation signal."""
    return bool(_USER_REPORT_RE.search(text or ""))


# A LEADING move-on / retraction cue: the user is abandoning the prior concern ("anyways do X", "forget
# that", "new topic"). An OPEN USER REPORT is a blocker on THAT concern — a real topic change clears it
# (see session.apply_turn_continuation), so a stale report can't hijack the fresh directive. The router is
# an LLM call biased to 'continue' and may miss the switch; this deterministic cue is the reliable backstop.
_REPORT_RETRACTED_RE = re.compile(
    r"^\s*(?:ok(?:ay)?\s*[,;:]?\s*)?(?:so\s+)?(?:"
    r"anyway|anyways|regardless|never\s*mind|nvm|scratch\s+that|"
    r"forget\s+(?:it|that|the|about)\b|drop\s+(?:it|that)\b|"
    r"(?:let'?s\s+)?move\s+on|moving\s+on|(?:let'?s\s+)?do\s+something\s+else|"
    r"new\s+(?:topic|task|thing)|different\s+(?:topic|task|thing)|change\s+(?:of\s+)?(?:topic|subject)|"
    r"instead\b|on\s+to\b)",
    re.I,
)


def report_retracted(text: str) -> bool:
    """True when a message opens with an explicit move-on cue that abandons the prior reported concern."""
    return bool(_REPORT_RETRACTED_RE.match(text or ""))


def capture_user_report(s, message: str) -> bool:
    """If `message` looks like a failure report, store it (verbatim, bounded) as the OPEN USER REPORT
    blocker on the slice and return True. A NEWER report replaces an older one (most-recent wins,
    inherently bounded). Returns False (and leaves any prior report intact) for a non-report message —
    so a benign follow-up does NOT clear a still-open report.

    The CAPTURING turn also shows the message in full via CURRENT REQUEST (no cap there); the risk is a
    LATER turn, where this bounded field is the only surviving copy — see _cut_with_recall_marker."""
    if not is_user_report(message):
        return False
    s.open_report = _cut_with_recall_marker(message, MAX_REPORT_CHARS)
    return True


# ── REGION_ORDER — the slice layout, region-by-region ─────────────────────────
# The slice is an address space of TYPED REGIONS. REGION_ORDER encodes their EXACT render order and
# the stable/volatile split that governs prompt-cache locality. A prefix cache matches only up to the
# first byte that differs from the previous request, so the STABLE BULK (OPEN FILES, RELATED CODE,
# skills, memory, conversation — byte-identical across the common read-only / reasoning steps) LEADS,
# and the VOLATILE tier (findings, RECENT, error — changes most steps) is
# the recency-salient TAIL: the immediate state and the high-authority blocker/error sit right above
# NOW. Each region renders its OWN framed fragment (header + body + spacing) and SUPPRESSES itself
# when empty (returns ''); render_regions joins the fragments. This replaces render_slice's
# hand-ordered parts[] list — the iteration MUST equal the old concatenation byte-for-byte.
#
# `slot` groups fragments into the original parts[] elements (fragments in the same slot are
# concatenated, in REGION_ORDER order, into one "\n".join part); the slot sequence + blank-line
# glue is fixed in render_regions. `tier` documents the stable/volatile split.
STABLE, VOLATILE = "stable", "volatile"


@dataclass(frozen=True)
class RegionSpec:
    """ONE declarative record per region — the single registration point.

    Every field is REQUIRED (no defaults): a region literally cannot be constructed
    half-registered. The three legacy tables (REGION_ORDER, _REGION_META, _REGION_ROLES)
    are DERIVED from REGIONS below — never hand-edit them again. This kills the drift
    class where a region was in the render order but fell through the _REGION_META /
    _REGION_ROLES .get(...) defaults (that fallthrough silently gave Tier-1 `corrections`
    generic priority-50 non-mandatory metadata). The runtime .get defaults REMAIN as the
    monkeypatch seam for tests that inject fake regions; completeness for REAL regions is
    enforced by tests/test_region_registry.py, not by strict indexing."""
    name: str
    tier: str                     # STABLE | VOLATILE — prompt-cache locality (documentation)
    render: Callable[[dict], str]  # ctx -> framed fragment; '' suppresses the region
    slot: int                     # parts[] grouping; render order = tuple position
    priority: int                 # elasticity degradation rank (lowest degrades first)
    instruction_class: InstructionClass
    freshness: FreshnessClass
    mandatory: bool               # lossless-only; named in ContextUnfitError; no locator alternative
    role: EpistemicRole


# Each region is (name, tier, render(ctx)->framed-fragment, slot). The renderer OWNS its header
# literal + spacing and SUPPRESSES itself (returns '') when empty. `tier` documents the
# stable-bulk/volatile-tail split (prompt-cache locality). `slot` maps the fragment onto the former
# CURRENT REQUEST (the live user ask) and the NOW footer render OUTSIDE the <context> envelope in
# slice.build() — NOT as REGION_ORDER entries. The envelope marks "reference STATE"; the live INSTRUCTION must
# frame it once from OUTSIDE at the recency-salient tail, with NOW as the outermost tail. Repeating a leading
# premise at primacy made one utterance look like two corroborating context items.
_CURRENT_REQUEST_HDR = ("# CURRENT REQUEST (what the user is asking for RIGHT NOW — your PRIMARY instruction; "
                        "address THIS)\n")
_NOW_FOOTER = ("# NOW: address the CURRENT REQUEST above. If it asks a QUESTION or for an explanation, answer "
               "it directly (observation tools may ground the answer). If it asks for action, use reasonable "
               "reversible judgment to carry it through within the exact user constraints; ask only when a "
               "material ambiguity would change the result or before an unclear consequential external action. "
               "Base changes on OPEN FILES; once the request is fully handled and verified "
               "as well as the environment allows, write your final summary and make NO tool call.")


def render_current_request(goal: str) -> str:
    """The live user ask, rendered once OUTSIDE the context fence at the salient tail.

    Empty goal → '' (no header).
    """
    g = str(goal or "")
    return f"{_CURRENT_REQUEST_HDR}{g}\n\n" if g.strip() else ""


def render_now(hints: str = "") -> str:
    """The intent-aware NOW footer — the OUTERMOST tail (after the fence closes), so the final instruction
    reads as an instruction, not as 'context'. `hints` = pre-framed SUBDIRECTORY CONTEXT prefix (may be '')."""
    return (hints or "") + _NOW_FOOTER


# parts[] grouping: fragments sharing a slot are concatenated, in order, into one "\n".join part —
# so the iteration equals the old hand-ordered concatenation BYTE-FOR-BYTE. (Provenance framing for
# # YOUR NOTES / the # OPEN USER REPORT blocker / the # REPEATED-FAILING header all live in the
# literals below — relocated verbatim from render_slice, not duplicated.)
# Field order: RegionSpec(name, tier, render, slot, priority, instruction_class, freshness, mandatory, role)
REGIONS: tuple[RegionSpec, ...] = (
    # ──────────── TIER 1 · INTENT — what the user wants (the contract). STABLE, slot-0: leads the cache prefix. ────────────
    # ACTIVE INTENT — exact standing clauses with typed lifecycle. EMPTY by default, so a greeting/question
    # produces no false contract. There is deliberately no semantic count/character cap here: physical
    # pressure changes representation later, never by silently dropping obligations in this reducer.
    RegionSpec("intent",         STABLE,   lambda c: (f"# ACTIVE USER INTENT (verbatim user-authored obligations that still govern this task; '[~]' is only provisional, not user-finalized)\n{render_intent(c['s'].intent, authorities=('user',))}\n\n" if render_intent(getattr(c['s'], 'intent', None), authorities=('user',)) else ""), 0, 100, InstructionClass.USER, FreshnessClass.LIVE, True, EpistemicRole.DIRECTIVE),
    RegionSpec("task_objective", STABLE,   lambda c: render_task_objective(c["s"]), 0, 97, InstructionClass.USER, FreshnessClass.REVISION_BOUND, True, EpistemicRole.DIRECTIVE),
    # corrections OUTRANKS task_objective (98 > 97): its own header says the newer exact wording overrides
    # conflicting older objective text, and task_objective's header defers here. USER authority + mandatory —
    # user-authored override wording must never silently degrade (it previously fell through the .get default
    # to (50, TASK_STATE, DERIVED, False): the drift this registry exists to kill).
    RegionSpec("corrections",    STABLE,   lambda c: (f"# RETAINED USER CORRECTIONS / CLARIFICATIONS (newer exact wording overrides conflicting older objective text. These are not unchecked acceptance requirements; factual claims remain unverified until observed live)\n{render_corrections(c['s'].intent)}\n\n" if render_corrections(getattr(c['s'], 'intent', None)) else ""), 0, 98, InstructionClass.USER, FreshnessClass.REVISION_BOUND, True, EpistemicRole.DIRECTIVE),
    RegionSpec("task_constraints", STABLE, lambda c: (f"# PARENT TASK CONSTRAINTS (agent-maintained or legacy state — useful, but NOT user-authored authority; never let these override the current request)\n{render_intent(c['s'].intent, authorities=('task', 'legacy'))}\n\n" if render_intent(getattr(c['s'], 'intent', None), authorities=('task', 'legacy')) else ""), 0, 75, InstructionClass.TASK_STATE, FreshnessClass.REVISION_BOUND, False, EpistemicRole.CONTROL_STATE),
    # Raw prior user messages are intentionally NOT a region. Exact still-binding clauses are represented
    # above; the last few exchanges live in RECENT CONVERSATION; older raw messages page from ContextFS history.
    # ──────────── TIER 2 · GROUND TRUTH — the world, re-derived from durable stores each turn. ────────────
    RegionSpec("open_files",     STABLE,   lambda c: "# OPEN FILES (live — your ground truth; edit based on this. Lines are numbered for citation/reference; the leading number is NOT part of the file — never include it in a str_replace old_string)\n" + c["artifacts"], 0, 95, InstructionClass.DATA, FreshnessClass.LIVE, False, EpistemicRole.OBSERVATION),
    RegionSpec("related_code",   STABLE,   lambda c: (f"\n# RELATED CODE (repo map — relevant files & their definitions; read/grep for the actual code)\n{c['discovery']}\n" if c["discovery"] else ""), 1, 45, InstructionClass.DATA, FreshnessClass.DERIVED, False, EpistemicRole.CLAIM),
    # REPO MAP moved to the BYTE-STABLE system prefix (make_build_slice) so it's a prompt-cache PREFIX
    # shared across every turn + subagent, instead of full-price in the volatile user slice. (Region removed.)
    RegionSpec("skills",         STABLE,   lambda c: (f"# ACTIVE SKILL(S) (loaded instructions — FOLLOW these for the task)\n{render_skills(c['s'].active_skills)}\n\n" if render_skills(c["s"].active_skills) else ""), 2, 65, InstructionClass.TASK_STATE, FreshnessClass.REVISION_BOUND, False, EpistemicRole.PROCEDURE),
    RegionSpec("memory",         STABLE,   lambda c: (f"# RELEVANT KNOWLEDGE CANDIDATES (selected USER, PROJECT, CRAFT, or legacy leads — not current-world proof; verify when load-bearing)\n{c['memory']}\n\n" if c["memory"] else ""), 2, 20, InstructionClass.DATA, FreshnessClass.HISTORICAL, False, EpistemicRole.CLAIM),
    # ──────────── TIER 3 · MY STATE — what the agent has established / is doing. ────────────
    RegionSpec("conversation",   STABLE,   lambda c: (f"# RECENT CONVERSATION (the last few exchanges this session — for continuity; older turns are paged out — see PAGED-OUT HISTORY below for the read_file(\"@sliceagent/history/turn-N.md\") call to fetch each)\n{render_conversation(c['s'])}\n\n" if render_conversation(c["s"]) else ""), 2, 80, InstructionClass.USER, FreshnessClass.HISTORICAL, False, EpistemicRole.CLAIM),
    RegionSpec("findings",       VOLATILE, lambda c: (f"# YOUR NOTES FROM PRIOR TOOL CALLS (task-scoped observations and claims to REUSE as leads; OPEN FILES stays ground truth for current contents. Per-note tags mark trust: no tag = observed, '(your note)' = summary, '(UNVERIFIED claim)' = not confirmed)\n{render_findings(c['s'].findings[-c['max_findings']:], c['s'].finding_source)}\n\n" if render_findings(c["s"].findings[-c["max_findings"]:], c["s"].finding_source) else ""), 3, 82, InstructionClass.TASK_STATE, FreshnessClass.REVISION_BOUND, False, EpistemicRole.CLAIM),
    # progress/world carry CLAIM (not the CONTROL_STATE fallback they used to inherit): both are the model's
    # own carried-forward assertions — same epistemic status as findings — never live observation.
    RegionSpec("progress",       VOLATILE, lambda c: (f"# PROGRESS SIGNALS (small task-scoped observations carried across turns; exact detail remains in @sliceagent/history/)\n{render_progress_signals(c['s'].task.progress_signals)}\n\n" if render_progress_signals(c['s'].task.progress_signals) else ""), 3, 35, InstructionClass.TASK_STATE, FreshnessClass.HISTORICAL, False, EpistemicRole.CLAIM),
    RegionSpec("world",          VOLATILE, lambda c: (f"# WORLD MODEL (durable task state YOU maintain — your map / inventory / progress; update with world_set, it persists across turns until the task changes)\n{render_world(c['s'].world)}\n\n" if c['s'].world else ""), 3, 85, InstructionClass.TASK_STATE, FreshnessClass.REVISION_BOUND, False, EpistemicRole.CLAIM),
    # ──────────── TIER 4 · RECALL — paged out of the slice; fetched on demand. ────────────
    RegionSpec("threads",        VOLATILE, lambda c: (f"# OTHER OPEN THREADS (parked topics — resume one with switch_topic; do NOT mix them into the current task)\n{c['threads']}\n\n" if c["threads"] else ""), 3, 25, InstructionClass.TASK_STATE, FreshnessClass.DERIVED, False, EpistemicRole.LOCATOR),
    # PAGED-OUT HISTORY — the cache MANIFEST: earlier turns of THIS session that are NOT in the slice,
    # each with the exact @sliceagent/history/ read_file call to page it back. Sits beside GHOST INDEX
    # (same "it's paged out, here's the one call to get it"
    # idiom) so the model has a SEEN target to read; an unseen cache is the dead channel. Locators only.
    RegionSpec("cache_manifest", VOLATILE, lambda c: (f"\n# PAGED-OUT HISTORY (canonical exact evidence from earlier turns, not current-world truth; read a turn with the shown @sliceagent/history/ locator, read_file(\"@sliceagent/history/index.md\") for the full list, or search_history(\"keywords\") across sessions)\n{c['cache_manifest']}\n" if c.get("cache_manifest") else ""), 3, 30, InstructionClass.DATA, FreshnessClass.HISTORICAL, False, EpistemicRole.LOCATOR),
    # ──────────── TIER 5 · LIVE STATE — what's wrong / where things stand (VOLATILE, high-authority tail). ────────────
    # (The REPEATED/FAILING ACTIONS header + tally regions were deleted 2026-08-03 — render-dead at
    # seed time; the anti-loop advisory rides the message channel, and the surviving action-log FOLD
    # below serves failure identity/supersession, which that advisory does NOT consume.)
    # (CURRENT REQUEST renders OUTSIDE the fence in build() — see render_current_request above — not here.)
    RegionSpec("turn_contract",  VOLATILE, lambda c: (
        f"# TURN CONTRACT (host-derived grounding and evidence plan for the exact CURRENT REQUEST; this "
        f"guides context selection and does not replace the user's words or your reasonable judgment)\n"
        f"{render_turn_contract(c['s'])}\n\n"
        if render_turn_contract(c["s"]) else ""), 6, 100, InstructionClass.USER, FreshnessClass.LIVE, True, EpistemicRole.CONTROL_STATE),
    # REPO STATE — the LIVE world-state region (SENSORY CORTEX — a derived view, tier A): current branch
    # + changed-file set, re-probed every build (not the session-start snapshot, and never persisted).
    # High-authority current-state ground truth, so it rides in the salient tail just above the blocker/
    # error. Suppresses itself when not a repo.
    # CURRENT PROJECT — where the agent is working RIGHT NOW (the frame on top of the immutable boundary):
    # the moved relative-path base + auto-granted file-tool reach, otherwise invisible. Rides the salient
    # tail so a follow-up's referent resolves HERE. Self-suppresses for the single-project case.
    RegionSpec("focus",          VOLATILE, lambda c: (f"# CURRENT PROJECT (where you are working RIGHT NOW — bare relative paths resolve here and your file tools reach here)\n{c['focus']}\n\n" if c.get("focus") else ""), 6, 78, InstructionClass.DATA, FreshnessClass.LIVE, False, EpistemicRole.OBSERVATION),
    RegionSpec("worktree",       VOLATILE, lambda c: (f"# REPO STATE (LIVE — current branch & changed files, re-read THIS turn; this is the up-to-date git state — trust it over any session-start project facts)\n{c['worktree']}\n\n" if c.get("worktree") else ""), 6, 92, InstructionClass.DATA, FreshnessClass.LIVE, False, EpistemicRole.OBSERVATION),
    # OPEN USER REPORT rides ABOVE the error (a stale "done" note can't outrank a user's BROKEN report);
    # both are the highest-authority, freshest tail right above NOW.
    RegionSpec("user_report",    VOLATILE, lambda c: (f"# OPEN USER REPORT (the user reports this is BROKEN — treat it as an UNRESOLVED blocker; do NOT claim it is done or already working until you have VERIFIED the fix against the real artifact, e.g. run/open it and observe success)\n{c['s'].open_report}\n\n" if c["s"].open_report else ""), 6, 99, InstructionClass.USER, FreshnessClass.LIVE, True, EpistemicRole.CLAIM),
    RegionSpec("reconciliation", VOLATILE, lambda c: render_reconciliation(c["s"]), 6, 100, InstructionClass.TASK_STATE, FreshnessClass.LIVE, True, EpistemicRole.CONTROL_STATE),
    RegionSpec("error",          VOLATILE, lambda c: (f"# CURRENT ERROR (unresolved — fix this, verbatim)\n{c['s'].last_error}\n\n" if c["s"].last_error else ""), 6, 98, InstructionClass.TASK_STATE, FreshnessClass.LIVE, True, EpistemicRole.OBSERVATION),
    # (NOW footer renders OUTSIDE the fence as the outermost tail in build() — see render_now above — not here.)
)


# Derived legacy views — the ONLY definitions of these three names; every existing consumer
# (build_context_blocks, render_context_selection, external tests, the sliceagent.regions shim)
# keeps working on the identical shapes. Never hand-edit these: edit REGIONS above. The stale
# _REGION_META["plan"] key died in this merge (no "plan" region exists in the render order).
REGION_ORDER = tuple((r.name, r.tier, r.render, r.slot) for r in REGIONS)
_REGION_META = {r.name: (r.priority, r.instruction_class, r.freshness, r.mandatory) for r in REGIONS}
_REGION_ROLES = {r.name: r.role for r in REGIONS}


def render_regions(ctx: dict) -> str:
    """Iterate REGION_ORDER, render each typed region into its framed fragment, and assemble the ONE
    user string (the moat). Each region suppresses itself when empty; the slot grouping keeps the
    stable bulk leading for prompt-cache locality and the volatile salient tail (slot 6) trailing. `ctx` carries the Slice + the
    pre-rendered passthroughs (artifacts / discovery / memory / threads) + the max_findings cap."""
    blocks = build_context_blocks(ctx)
    selection = ElasticityController().select(blocks)
    return render_context_selection(selection)


def _locator_region(name: str, ctx: dict) -> tuple[str, tuple[str, ...], bool] | None:
    """Return a smaller faithful locator only where refinement/re-observation is real."""
    s = ctx.get("s")
    if name == "task_objective":
        source = str(getattr(getattr(s, "task", None), "goal_source", "") or "").strip()
        handle = f"artifacts/{source}.md" if source else "artifacts/index.md"
        return (f'# PRIOR TASK BACKGROUND\n- read_file("{handle}") for the original objective',
                (handle,), False)
    if name == "open_files":
        paths = tuple(dict.fromkeys(ctx.get("open_file_paths", getattr(s, "active_files", ())) or ()))
        body = "\n".join(f'- read_file("{path}")' for path in paths)
        return ("# OPEN FILES (paged under context pressure — re-read live before acting)\n"
                + (body or "(no resident file body)"), paths or ("workspace",), True)
    if name == "related_code":
        return ("# RELATED CODE (derived view omitted under pressure — use grep/glob on the live repo)\n"
                "(re-observe when needed)", ("workspace",), True)
    if name == "skills":
        names = tuple(str(item.get("name")) for item in getattr(s, "active_skills", ()) if item.get("name"))
        return ("# ACTIVE SKILL(S) (bodies paged under pressure; reload with the skill tool)\n"
                + "\n".join(f"- {item}" for item in names), names or ("skill-catalog",), True)
    if name == "memory":
        return ("# RELEVANT KNOWLEDGE CANDIDATES (historical leads omitted under pressure; re-query if needed)\n"
                '- read_file("@sliceagent/memory/index.md") or rebuild the next seed',
                ("@sliceagent/memory/index.md",), True)
    if name == "conversation":
        handles = tuple(
            f"artifacts/{row.get('artifact_id')}.md" for row in getattr(s, "conversation", ())[:-1]
            if row.get("artifact_id")
        ) or ("artifacts/index.md",)
        return ("# RECENT CONVERSATION (paged under pressure; exact turns remain in the artifact/history view)\n"
                + "\n".join(f'- read_file("{handle}")' for handle in handles), handles, False)
    if name == "turn_contract":
        contract = getattr(getattr(s, "intent", None), "turn_contract", None)
        handles = tuple(dict.fromkeys(
            f"artifacts/{artifact_id}.md"
            for ref in (getattr(contract, "referents", ()) or ())
            if (artifact_id := str(getattr(getattr(ref, "anchor", None), "artifact_id", "") or ""))
        ))
        grounding = str(getattr(contract, "grounding", "none") or "none")
        return (
            "# TURN CONTRACT (grounding/evidence detail paged under pressure; exact user constraints remain)\n"
            f"- grounding: {grounding}\n"
            + ("\n".join(f'- read_file("{handle}")' for handle in handles)
               if handles else "- no resolved artifact handle"),
            handles or ("current-request",), False,
        )
    if name == "findings":
        return ('# YOUR NOTES FROM PRIOR TOOL CALLS (paged under context pressure)\n'
                '- read_file("artifacts/index.md") and refine the relevant sealed turn',
                ("artifacts/index.md",), False)
    if name == "progress":
        return ("# EXECUTION PROGRESS (detail paged under pressure)\n"
                '- read_file("artifacts/index.md") for sealed turn detail', ("artifacts/index.md",), False)
    if name == "threads":
        return ("# OTHER OPEN THREADS (details omitted under pressure; switch_topic by task id to refine)\n"
                + str(ctx.get("threads") or ""), ("task-checkpoints",), True)
    if name == "cache_manifest":
        return ('# PAGED-OUT HISTORY\n- read_file("@sliceagent/history/index.md") for the full manifest',
                ("@sliceagent/history/index.md",), False)
    if name == "focus":
        return ("# CURRENT PROJECT (live locator)\n" + str(ctx.get("focus") or ""),
                ("workspace",), True)
    if name == "worktree":
        return ("# REPO STATE (live view omitted under pressure — re-run git status before relying on it)",
                ("workspace",), True)
    return None


_SEALED_SOURCE_REGIONS = frozenset({
    # User/task wording needed to judge compliance or response quality.
    "intent", "task_objective", "corrections", "task_constraints", "conversation",
    # Exact/archive recovery.
    "cache_manifest", "turn_contract",
    # Subject continuity plus explicit user reports/execution uncertainty remain visible.
    "focus", "user_report", "reconciliation",
})


def _region_selected_by_source_needs(name: str, ctx: dict) -> bool:
    """Preselect semantic sources before elasticity chooses their physical fidelity.

    A pure sealed-execution question should not receive every roomy code, plan, note, and diagnostic region;
    that furniture is neither requested nor proof and was a major confabulation cue in the self-audit A/B.
    Mixed/live questions and effectful turns retain the full task slice. This is relevance routing, not a size
    bound: every selected region can still accumulate elastically within the slice.
    """
    contract = getattr(getattr(ctx.get("s"), "intent", None), "turn_contract", None)
    if contract is None:
        return True
    needs = set(getattr(contract, "source_needs", ()) or ())
    if not needs:
        return True
    if "current_world" in needs or getattr(contract, "effect_authority", "none") in {
        "explicit", "continuation",
    }:
        return True
    if (name == "conversation" and "sealed_exchange" in needs
            and not getattr(contract, "evidence_continuation", False)):
        # The quality projection already contains the exact paired bytes. Duplicate recent pairs gave those
        # claims accidental extra weight; only a verification continuation keeps RECENT for the assessment
        # response itself, which is intentionally outside the frozen historical baseline.
        return False
    selected = set(_SEALED_SOURCE_REGIONS)
    if "historical_observation" in needs:
        selected.update(("findings", "memory"))
    return name in selected


def _region_provenance(name: str, ctx: dict) -> tuple[EpistemicRole, tuple[str, ...],
                                                       tuple[SourceRef, ...], tuple[ResourceRef, ...]]:
    """Attach source identity without making the renderer another writable state store."""
    s = ctx.get("s")
    role = _REGION_ROLES.get(name, EpistemicRole.CONTROL_STATE)
    scope = ("task",)
    sources: list[SourceRef] = []
    resources: list[ResourceRef] = []

    if name in {"intent", "turn_contract", "corrections"}:
        handle = str(getattr(getattr(s, "intent", None), "current_source", "") or "current-request")
        sources.append(SourceRef("user_utterance", handle))
        scope = ("turn", "task")
    elif name == "task_objective":
        handle = str(getattr(getattr(s, "task", None), "goal_source", "") or "task-objective")
        sources.append(SourceRef("user_utterance", handle))
    elif name == "open_files":
        scope = ("workspace", "task")
        for path in dict.fromkeys(ctx.get("open_file_paths", getattr(s, "active_files", ())) or ()):
            # The seed supplied these through the live host classifier, so even a handle spelled
            # `artifacts/x.md` is a physical workspace file when a real mount shadows the virtual view.
            ref = ResourceRef(ResourceKind.WORKSPACE_FILE, str(path))
            resources.append(ref)
            sources.append(SourceRef("live_resource", ref.handle))
    elif name == "conversation":
        scope = ("session", "task")
        for row in getattr(s, "conversation", ()) or ():
            handle = str(row.get("artifact_id") or "") if isinstance(row, dict) else ""
            if handle:
                sources.append(SourceRef("artifact", handle))
    elif name == "cache_manifest":
        scope = ("session",)
        ref = reserved_resource_ref("history/index.md")
        resources.append(ref); sources.append(SourceRef("historical_view", ref.handle))
    elif name == "skills":
        for item in getattr(s, "active_skills", ()) or ():
            handle = str(item.get("name") or "") if isinstance(item, dict) else ""
            if handle:
                resources.append(ResourceRef(ResourceKind.SKILL, handle))
                sources.append(SourceRef("procedure", handle))
    elif name in {"focus", "worktree", "related_code"}:
        scope = ("workspace", "turn")
        sources.append(SourceRef("live_resource" if role is EpistemicRole.OBSERVATION else "derived_view",
                                 "workspace"))
    elif name in {"memory", "threads"}:
        scope = ("cross_session",) if name == "memory" else ("session",)
        sources.append(SourceRef("historical_view" if name == "memory" else "task_state", name))
    else:
        sources.append(SourceRef("task_state", name))
    return role, scope, tuple(dict.fromkeys(sources)), tuple(dict.fromkeys(resources))


def _ring_within_reserve(s) -> bool:
    """True when the WHOLE conversation ring fits the verbatim reserve budget (then the legacy
    conversation region is soft-reserved; an over-budget ring degrades normally)."""
    rows = tuple(getattr(s, "conversation", ()) or ())
    if not rows:
        return False
    total = sum(len(str(r.get("user") or "")) + len(str(r.get("assistant") or "")) for r in rows)
    return total <= user_reserve_chars()


def build_context_blocks(ctx: dict) -> tuple[ContextBlock, ...]:
    """Project every non-empty region into the shared elasticity contract."""
    out = []
    for order, (name, _tier, render, slot) in enumerate(REGION_ORDER):
        if not _region_selected_by_source_needs(name, ctx):
            continue
        content = render(ctx)
        if not content:
            continue
        priority, authority, freshness, mandatory = _REGION_META.get(
            name, (50, InstructionClass.TASK_STATE, FreshnessClass.DERIVED, False))
        if (name == "task_objective"
                and getattr(getattr(ctx.get("s"), "task", None), "objective_status", "active")
                == "provisionally_satisfied"):
            # Same topic does not mean "redo the original request".  Once a clean turn provisionally
            # completes it, retain it as lower-authority, pageable background until an explicit resume or
            # failure report reactivates it.
            priority, authority, freshness, mandatory = (
                28, InstructionClass.TASK_STATE, FreshnessClass.HISTORICAL, False,
            )
        if name == "conversation" and _ring_within_reserve(ctx.get("s")):
            # VERBATIM USER RESERVE, legacy (no-graph) lane — mirrors _adjacency_blocks' reserved
            # priority so behavior is lane-independent (the known path-asymmetry bug class). SOFT:
            # the locator alternative below stays available as the true last resort. A ring whose
            # total chars exceed the budget (giant pastes inside the floor) keeps normal priority
            # and degrades like any region.
            priority = RESERVE_PRIORITY
        group = f"region:{name}"
        role, scope, source_refs, resource_refs = _region_provenance(name, ctx)
        out.append(ContextBlock(
            block_id=f"{group}:full", item_id=group, alternative_group=group,
            priority=priority, instruction_class=authority, freshness=freshness,
            fidelity=Fidelity.FULL, representation_loss=RepresentationLoss.NONE,
            content=content, mandatory=mandatory, order=order, slot=slot,
            epistemic_role=role, scope=scope, source_refs=source_refs,
            resource_refs=resource_refs,
        ))
        locator = None if mandatory else _locator_region(name, ctx)
        if locator is not None and len(locator[0]) < len(content):
            locator_content, handles, reobservable = locator
            out.append(ContextBlock(
                block_id=f"{group}:locator", item_id=group, alternative_group=group,
                priority=priority, instruction_class=authority, freshness=freshness,
                fidelity=Fidelity.LOCATOR, representation_loss=RepresentationLoss.POINTER_ONLY,
                content=locator_content, handles=tuple(handles), reobservable=reobservable,
                order=order, slot=slot,
                epistemic_role=EpistemicRole.LOCATOR, scope=scope,
                source_refs=tuple(dict.fromkeys((*source_refs, *(
                    SourceRef("locator", str(handle)) for handle in handles
                )))),
                resource_refs=tuple(dict.fromkeys((*resource_refs, *(
                    reserved_resource_ref(str(handle)) for handle in handles
                )))),
            ))
    return tuple(out)


def render_context_selection(selection: ContextSelection) -> str:
    """Render one selected alternative per region using the existing stable slot layout."""
    slots: dict[int, str] = {}
    for block in selection.blocks:
        slots[block.slot] = slots.get(block.slot, "") + block.content
    if not REGION_ORDER:
        return ""
    # #17: assemble by iterating ALL slot positions rather than a hand-synced literal index list — that
    # list KeyError'd if a leading slot was empty and SILENTLY DROPPED any region added at a gap slot
    # (e.g. 5). Slot 5 stays the reserved blank separator between the stable bulk (≤4, cache-leading) and
    # the volatile high-authority tail (≥6); an empty slot renders as "" (a blank line), as before.
    max_slot = max(entry[3] for entry in REGION_ORDER)
    return "\n".join(slots.get(i, "") for i in range(max_slot + 1))
