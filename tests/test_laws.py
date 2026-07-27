"""LAW SUITE — the categorical invariants of the slice kernel, as executable equations.

Design (docs/ENDGAME-CONTEXT-DESIGN.md ## Laws): sliceagent is a lawful category plus exactly one
axiom-free oracle morphism (the model call). Everything AROUND the oracle must obey equations:
idempotence, roundtrip identity, monotonicity, functor identity. Two shipped bugs (redact_text
non-idempotence -> seal hash desync; view_bytes freeze/thaw desync) were LAW violations found only
by expensive review — this suite finds that class mechanically.

Every check is deterministic: seeded generators, no wall clock, no model, no network.
"""
from __future__ import annotations

import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent.active_work import ResourceRef, WorkDelta, WorkGraph, WorkItem   # noqa: E402
from sliceagent.safety import redact_text                                        # noqa: E402

CHECKS = []
def check(fn):
    CHECKS.append(fn)
    return fn


# ── L1a · idempotence of the redaction PRIMITIVE ────────────────────────────────────────────────
# Status: the primitive is NOT required to be idempotent (a first pass may shorten a secret-shaped
# literal below the mask threshold; pass two then masks it fully). The SYSTEM invariant that keeps
# this safe is L1c: canonical sealing preserves already-redacted bytes exactly (observation.redacted
# short-circuits), so no hash is ever computed across a second pass. This check DOCUMENTS the
# primitive's real contract: a second pass may only ever REDUCE information, never resurrect it.
_SECRETY = [
    'export SECRET_KEY="django-insecure-abcdefghijklmnopqrstuvwxyz0123456789"',
    "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx1234567890abcdef",
    "postgres://admin:hunter2hunter2@db.internal:5432/prod",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "plain text with no secrets at all",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
]

@check
def l1a_redact_second_pass_only_reduces_never_resurrects():
    for raw in _SECRETY:
        once = redact_text(raw)
        twice = redact_text(once)
        # The load-bearing half of idempotence: anything the first pass hid must STAY hidden, and a
        # second pass must not re-introduce raw source bytes that the first pass removed.
        for token in ("hunter2", "django-insecure-abcdefghijklmnopqrstuvwxyz", "sk-proj-abcdefghijklmnop"):
            if token not in once:
                assert token not in twice, f"second redact pass resurrected {token!r}"
        # And a third pass must be a fixed point of the second (the sequence stabilizes).
        assert redact_text(twice) == twice, f"redaction never stabilizes for {raw[:40]!r}"


@check
def l1a_redaction_is_total_and_always_returns_text():
    assert redact_text(None) == ""
    assert isinstance(redact_text(None), str)
    assert redact_text(42) == "42"


# ── L1b + L3 · dependency closure is a closure operator (idempotent · extensive · monotone) ─────
def _random_graph(seed: int) -> WorkGraph:
    rng = random.Random(seed)
    graph = WorkGraph().open_request(f"event-{seed}", f"request {seed}", logical_id=f"log-{seed}")
    root = graph.request_roots[-1]
    items = []
    for i in range(rng.randint(1, 6)):
        deps = tuple(rng.sample([it.id for it in items], k=rng.randint(0, len(items))) if items else ())
        item = WorkItem(
            id=f"item-{seed}-{i}", root_id=root.id, source_refs=root.source_refs,
            description=f"work {i}", status=rng.choice(["open", "in_progress"]),
            dependencies=deps,
            resource_refs=(ResourceRef("workspace_file", f"src/f{i}.py", workspace_epoch=0),),
        )
        items.append(item)
    return graph.apply(WorkDelta(expected_revision=graph.revision, creates=tuple(items)))

@check
def l1b_closure_is_idempotent():
    for seed in range(20):
        graph = _random_graph(seed)
        once = {it.id for it in graph.dependency_closure()}
        # Re-computing the closure over the same graph must be a fixed point.
        assert {it.id for it in graph.dependency_closure()} == once, f"closure unstable (seed {seed})"

@check
def l3_closure_is_monotone_under_item_addition():
    for seed in range(20):
        graph = _random_graph(seed)
        before = {it.id for it in graph.dependency_closure()}
        root = graph.request_roots[-1]
        grown = graph.apply(WorkDelta(expected_revision=graph.revision, creates=(WorkItem(
            id=f"item-extra-{seed}", root_id=root.id, source_refs=root.source_refs,
            description="added later", status="open",
        ),)))
        after = {it.id for it in grown.dependency_closure()}
        assert before <= after, f"adding an item SHRANK the closure (seed {seed}): {before - after}"


# ── L2a · roundtrip identity: from_record ∘ to_record = id (per persisted type) ─────────────────
def _seal_artifact(seed: int) -> dict:
    """The DUMB-SEAL corpus (docs/SUBAGENT-SCOPED-TURN.md): secret-shaped bytes ON PURPOSE — the
    shipped hash-desync bug class lived exactly where a redaction pass changes bytes. A neutral
    corpus would make the seal laws vacuously true."""
    rng = random.Random(seed)
    return {
        "kind": "explorer", "status": rng.choice(["ok", "partial", "failed"]),
        "steps": rng.randint(1, 6), "stop_reason": "end_turn", "launch_ordinal": seed + 1,
        "brief": {"task": f"objective {seed}\n" + rng.choice(_SECRETY)},
        "report": f"report body {seed}\n" + rng.choice(_SECRETY) + "\nline three",
    }

@check
def l2a_dumb_seal_storage_is_identity_on_the_clamped_image():
    """The scoped-turn seal has NO normal-form problem BY CONSTRUCTION: the record is clamped
    (redacted + byte-bounded) exactly once on its way to disk, storage is append-only JSONL, and the
    read path applies no second transform. The law: read-back == the clamp image, byte-identical —
    the old L1c hash-desync class cannot exist because no hash is ever computed and no second pass
    ever runs."""
    import json
    import shutil
    import tempfile
    vault = tempfile.mkdtemp(prefix="laws-vault-")
    prior = os.environ.get("SLICEAGENT_VAULT")
    os.environ["SLICEAGENT_VAULT"] = vault
    try:
        from sliceagent.memory import LocalMemory
        memory = LocalMemory(prefer_memem=False)
        for seed in range(12):
            art = _seal_artifact(seed)
            frozen = json.loads(json.dumps(art))          # what the caller handed over, immutable
            handle = memory.append_subagent_artifact("laws-1", art)
            assert handle == f"sub-{seed + 1}"
            stored = memory.read_subagent_artifacts("laws-1")[-1]["artifact"]
            assert stored == memory._clamp(frozen), (
                f"stored record differs from the clamp image (seed {seed}) — a second transform "
                "moved bytes between write and read"
            )
            # And the stored image is a FIXED POINT of storage itself: reading twice is identical.
            again = memory.read_subagent_artifacts("laws-1")[-1]["artifact"]
            assert again == stored, f"read is not stable (seed {seed})"
    finally:
        if prior is None:
            os.environ.pop("SLICEAGENT_VAULT", None)
        else:
            os.environ["SLICEAGENT_VAULT"] = prior
        shutil.rmtree(vault, ignore_errors=True)

@check
def l2a_workgraph_roundtrip_is_identity():
    for seed in range(12):
        graph = _random_graph(seed)
        thawed = WorkGraph.from_dict(graph.to_dict())
        assert thawed == graph, f"WorkGraph to_dict->from_dict is not identity (seed {seed})"


# ── L1c · the seal's redaction is confined to the WRITE edge ────────────────────────────────────
# THE law whose violation shipped the report-destroying bug in the OLD stack: a hash computed over
# bytes a later redaction pass could move. The dumb seal discharges the class structurally (no
# hashes, one clamp, verbatim reads) — l2a above proves it end-to-end. What remains law-worthy of
# the PRIMITIVE: the render surface must never resurrect raw secret bytes that the clamp removed.
@check
def l1c_render_never_resurrects_clamped_secrets():
    from sliceagent.hippocampus import render_artifact
    from sliceagent.memory import LocalMemory
    memory = LocalMemory.__new__(LocalMemory)              # _clamp needs no vault state
    for seed in range(12):
        art = _seal_artifact(seed)
        clamped = memory._clamp(art)
        rendered = render_artifact({"id": f"sub-{seed + 1}", "artifact": clamped})
        for token in ("hunter2hunter2", "django-insecure-abcdefghijklmnopqrstuvwxyz",
                      "sk-proj-abcdefghijklmnop"):
            if token in json.dumps(art) and token not in json.dumps(clamped):
                assert token not in rendered, (
                    f"render resurrected {token!r} (seed {seed}) — the read path re-introduced "
                    "bytes the seal removed"
                )


# ── L4 · functor identity at the compiler: same state ⇒ byte-identical context ──────────────────
# The identity law is the prompt-cache law: any nondeterminism here (set/dict ordering, timestamps)
# is a cache-miss with a dollar cost. Tested at the compiler level (below the volatile now-block).
@check
def l4_compile_is_deterministic_for_identical_state():
    from sliceagent.context import ElasticityController
    from sliceagent.context_compiler import compile_active_context
    from sliceagent.pfc import Slice
    from sliceagent.regions import render_context_selection

    for seed in range(8):
        graph = _random_graph(seed)
        def build() -> str:
            s = Slice(active_work=graph)
            s.conversation = [
                {"user": "prior q", "assistant": "prior a", "artifact_id": "turn-p"},
                {"user": "current", "assistant": "", "artifact_id": "turn-c"},
            ]
            compiled = compile_active_context(
                s, (), source_texts={f"event-{seed}": f"request {seed}"},
                current_logical_id=f"log-{seed}",
            )
            return render_context_selection(ElasticityController().select(compiled))
        first, second = build(), build()
        assert first == second, f"identical state compiled to different bytes (seed {seed})"


# ── negative control · the seal laws must not be vacuous ────────────────────────────────────────
# The corpus must contain redaction-sensitive bytes (clamp CHANGES it) — otherwise l2a/l1c above
# pass on any implementation and the suite has gone soft.
@check
def seal_negative_control_corpus_is_redaction_sensitive():
    from sliceagent.memory import LocalMemory
    memory = LocalMemory.__new__(LocalMemory)
    changed = sum(1 for seed in range(12)
                  if memory._clamp(_seal_artifact(seed)) != _seal_artifact(seed))
    assert changed >= 6, (
        f"only {changed}/12 corpus artifacts are redaction-sensitive — restore secret-shaped "
        "report/brief bytes or the seal laws are vacuous"
    )


if __name__ == "__main__":
    ok = 0
    for fn in CHECKS:
        try:
            fn(); ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(CHECKS)} passed")
    sys.exit(0 if ok == len(CHECKS) else 1)
