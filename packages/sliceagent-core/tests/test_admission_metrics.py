"""AdmissionMetrics — the admission-precision journal rows. No model/network.

Soundness contract under test: 'referenced' is a resource_observed deref JOIN only (never prose
matching); handle-less blocks land in 'unmatchable'; missed-need carries per-turn DELTAS of the
Slice's cumulative counters; a parked turn (TurnInterrupted) writes rows exactly like a clean one;
a broken provider never breaks the turn boundary."""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent_core.context import (ContextBlock, ContextSelection, Fidelity,  # noqa: E402
                                     FreshnessClass, InstructionClass, PressureLevel,
                                     RepresentationLoss)
from sliceagent_core.context_compiler import SOURCE_UNAVAILABLE_MARKER  # noqa: E402
from sliceagent_core.events import ToolResult, TurnEnd, TurnInterrupted  # noqa: E402
from sliceagent_core.execution import ToolEffect, ToolInvocation, ToolOutcome, ToolStatus  # noqa: E402
from sliceagent_core.records import AdmissionMetrics, Journal  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def _block(item_id, *, fidelity=Fidelity.FULL, content="body", handles=(), mandatory=False):
    loss = RepresentationLoss.NONE if fidelity is Fidelity.FULL else RepresentationLoss.POINTER_ONLY
    return ContextBlock(
        block_id=f"{item_id}:{fidelity.value}", item_id=item_id, alternative_group=item_id,
        priority=50, instruction_class=InstructionClass.DATA, freshness=FreshnessClass.DERIVED,
        fidelity=fidelity, representation_loss=loss, content=content,
        handles=tuple(handles), mandatory=mandatory, reobservable=not handles,
    )


def _plan(*blocks):
    return SimpleNamespace(last_selection=ContextSelection(
        blocks=tuple(blocks), pressure=PressureLevel.ROOMY, used_chars=1, capacity_chars=None))


def _deref_event(handle, artifact_id="", resource_kind="history"):
    payload = {"resource_kind": resource_kind, "handle": handle}
    if artifact_id:
        payload["artifact_id"] = artifact_id
    outcome = ToolOutcome(
        invocation=ToolInvocation(id="i1", name="read_file", args={"path": handle}, provider_index=0),
        status=ToolStatus.SUCCEEDED, text="content",
        effects=(ToolEffect(id="e1", kind="resource_observed", payload=payload),),
    )
    return ToolResult("read_file", {"path": handle}, "content", False, outcome=outcome)


def _sink(*blocks, slice_obj=None, plan=None):
    journal = Journal("t", root=tempfile.mkdtemp())
    provider = (lambda: plan) if plan is not None else (lambda: _plan(*blocks))
    return AdmissionMetrics(journal, provider, (lambda: slice_obj) if slice_obj is not None else None), journal


@check
def referenced_is_a_deref_join_and_handleless_blocks_are_unmatchable():
    m, journal = _sink(
        _block("region:conversation", handles=("artifacts/t3.md",)),
        _block("region:convergence"),                       # prose-only — no handle
        _block("region:open_files", handles=("src/a.py",)),  # handle, never dereffed
    )
    m(_deref_event("artifacts/t3.md", resource_kind="artifact"))
    m(TurnEnd("stop", 1, {}))
    row = journal.read("turn_regions")[0]
    assert row["referenced"] == ["region:conversation"], row
    assert row["unmatchable"] == ["region:convergence"], row
    assert set(row["admitted"]) == {"region:conversation", "region:convergence", "region:open_files"}
    assert row["derefs"] == 1
    per_block = {r["block"]: r for r in journal.read("admission")}
    assert per_block["region:convergence"]["matchable"] is False
    assert per_block["region:open_files"]["matchable"] is True


@check
def artifact_id_joins_via_the_artifacts_handle_convention():
    m, journal = _sink(_block("region:conversation", handles=("artifacts/abc123.md",)))
    m(_deref_event("history/turn-9.md", artifact_id="abc123", resource_kind="history"))
    m(TurnEnd("stop", 1, {}))
    assert journal.read("turn_regions")[0]["referenced"] == ["region:conversation"]


@check
def deref_of_degraded_flags_the_compiler_dropping_something_the_model_needed():
    m, journal = _sink(
        _block("region:findings", fidelity=Fidelity.LOCATOR, handles=("artifacts/index.md",)),
    )
    m(_deref_event("artifacts/index.md", resource_kind="artifact"))
    m(TurnEnd("stop", 1, {}))
    row = journal.read("turn_regions")[0]
    assert row["degraded"] == ["region:findings"]
    assert row["missed_need"]["deref_of_degraded"] == ["region:findings"], row
    assert row["missed_need"]["pageins"] == {"artifact": 1}


@check
def parked_turn_writes_rows_like_a_clean_one():
    m, journal = _sink(_block("region:intent", mandatory=True))
    m(TurnInterrupted("aborted"))
    row = journal.read("turn_regions")[0]
    assert row["ended"] == "interrupted" and row["admitted"] == ["region:intent"]
    assert journal.read("admission")[0]["mandatory"] is True


@check
def io_and_correction_deltas_are_baselined_on_first_sight():
    s = SimpleNamespace(
        io={"hit": 5, "miss": 2, "refault": 3, "evict": 1},
        intent=SimpleNamespace(entries=[SimpleNamespace(status="superseded")]),
    )
    m, journal = _sink(_block("region:intent"), slice_obj=s)
    m(TurnEnd("stop", 1, {}))
    first = journal.read("turn_regions")[0]["missed_need"]
    # A restored task's pre-existing cumulative counters must NOT spike turn 1.
    assert first["refault"] == 0 and first["corrections_superseded"] == 0, first
    s.io["refault"] = 5
    s.intent.entries.append(SimpleNamespace(status="superseded"))
    m(TurnEnd("stop", 1, {}))
    second = journal.read("turn_regions")[1]["missed_need"]
    assert second["refault"] == 2 and second["corrections_superseded"] == 1, second


@check
def missing_source_counts_the_compiler_marker_and_the_literal_matches():
    m, journal = _sink(
        _block("active-work", content=f"- [ ] w1 · request\n  {SOURCE_UNAVAILABLE_MARKER} — locator below"),
    )
    m(TurnEnd("stop", 1, {}))
    assert journal.read("turn_regions")[0]["missed_need"]["missing_source"] == 1
    assert SOURCE_UNAVAILABLE_MARKER == "exact source: UNAVAILABLE"  # records.py fallback literal


@check
def broken_providers_never_break_the_turn_boundary():
    journal = Journal("t", root=tempfile.mkdtemp())

    def _boom():
        raise RuntimeError("provider crashed")

    m = AdmissionMetrics(journal, _boom, _boom)
    m(TurnEnd("stop", 1, {}))
    row = journal.read("turn_regions")[0]
    assert row["admitted"] == [] and row["ended"] == "end"


@check
def derefs_reset_between_turns():
    m, journal = _sink(_block("region:conversation", handles=("artifacts/t1.md",)))
    m(_deref_event("artifacts/t1.md", resource_kind="artifact"))
    m(TurnEnd("stop", 1, {}))
    m(TurnEnd("stop", 1, {}))
    rows = journal.read("turn_regions")
    assert rows[0]["derefs"] == 1 and rows[1]["derefs"] == 0
    assert rows[1]["referenced"] == []


@check
def replay_seed_recorder_journals_messages_and_roster():
    from sliceagent_core.events import SliceBuilt
    from sliceagent_core.records import ReplaySeedRecorder
    journal = Journal("t", root=tempfile.mkdtemp())
    rec = ReplaySeedRecorder(journal, lambda: ["read_file", "grep"])
    rec(SliceBuilt("rendered user text", [
        {"role": "system", "content": "SYSTEM PREFIX"},
        {"role": "user", "content": [{"type": "text", "text": "multimodal user part"}]},
    ]))
    row = journal.read("replay_seed")[0]
    assert row["turn"] == 1 and row["roster"] == ["read_file", "grep"]
    assert row["messages"][0] == {"role": "system", "content": "SYSTEM PREFIX"}
    assert row["messages"][1]["content"] == "multimodal user part"   # parts flattened verbatim
    rec(SliceBuilt("r2", None))                                      # None messages: row still writes
    assert journal.read("replay_seed")[1]["messages"] == []


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
