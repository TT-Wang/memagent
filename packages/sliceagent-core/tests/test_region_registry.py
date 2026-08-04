"""RegionSpec registry exhaustiveness — a region can only exist FULLY registered.

The three legacy tables (REGION_ORDER, _REGION_META, _REGION_ROLES) are derived from the
single REGIONS tuple; this suite makes the old drift class (a region present in the render
order but falling through the .get defaults — the corrections priority-50 bug) unrepresentable:
construction is total (no-defaults dataclass), derivation is total (empty-diff checks),
side registries are policed (AST subset checks), and layout changes are explicit (golden
snapshot). No model/network."""
import ast
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent_core import regions as regions_mod  # noqa: E402
from sliceagent_core.context import (ElasticityController, EpistemicRole,  # noqa: E402
                                     Fidelity, FreshnessClass, InstructionClass,
                                     RepresentationLoss)
from sliceagent_core.regions import (_REGION_META, _REGION_ROLES, _SEALED_SOURCE_REGIONS,  # noqa: E402
                                     REGION_ORDER, REGIONS, STABLE, VOLATILE, RegionSpec)

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


NAMES = tuple(r.name for r in REGIONS)


@check
def names_are_unique():
    assert len(set(NAMES)) == len(REGIONS), sorted(n for n in set(NAMES) if NAMES.count(n) > 1)


@check
def derivation_is_total_no_drift():
    # Four explicit empty-diff checks so a failure NAMES the drifted region — these four sets
    # are exactly the drift classes observed before the merge (corrections / plan / 7 roles).
    names = set(NAMES)
    assert names - set(_REGION_META) == set(), f"in ORDER not META: {names - set(_REGION_META)}"
    assert set(_REGION_META) - names == set(), f"stale META keys: {set(_REGION_META) - names}"
    assert names - set(_REGION_ROLES) == set(), f"in ORDER not ROLES: {names - set(_REGION_ROLES)}"
    assert set(_REGION_ROLES) - names == set(), f"stale ROLES keys: {set(_REGION_ROLES) - names}"
    assert {t[0] for t in REGION_ORDER} == names


@check
def runtime_get_defaults_are_unreachable_for_real_regions():
    # Direct indexing (KeyError names the culprit). The .get defaults in build_context_blocks /
    # _region_provenance stay ONLY as the monkeypatch seam for fake test regions.
    for name in NAMES:
        _REGION_META[name]
        _REGION_ROLES[name]


@check
def field_domains():
    for r in REGIONS:
        assert isinstance(r.name, str) and r.name, r
        assert r.tier in (STABLE, VOLATILE), (r.name, r.tier)
        assert callable(r.render), r.name
        assert isinstance(r.slot, int) and 0 <= r.slot, (r.name, r.slot)
        assert isinstance(r.priority, int) and 0 <= r.priority <= 100, (r.name, r.priority)
        assert isinstance(r.instruction_class, InstructionClass), r.name
        assert isinstance(r.freshness, FreshnessClass), r.name
        assert isinstance(r.mandatory, bool), r.name
        assert isinstance(r.role, EpistemicRole), r.name


@check
def regionspec_has_no_defaults():
    # Half-registration must be a CONSTRUCTION error: every field required.
    import dataclasses
    for f in dataclasses.fields(RegionSpec):
        assert f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING, f.name


@check
def derived_views_share_the_same_objects():
    # Consumers depend on the positional 4-tuple shape AND on the raw renderer callables
    # (test_context_contract_eval calls row[2] directly) — never wrap them.
    for spec, entry in zip(REGIONS, REGION_ORDER):
        name, tier, render, slot = entry
        assert (spec.name, spec.tier, spec.slot) == (name, tier, slot)
        assert spec.render is render, spec.name
    for spec in REGIONS:
        assert _REGION_META[spec.name] == (spec.priority, spec.instruction_class,
                                           spec.freshness, spec.mandatory)
        assert _REGION_ROLES[spec.name] is spec.role


def _name_literals(fn) -> set:
    """String literals compared against `name` inside fn — `name == "x"` / `name in ("a", "b")`."""
    tree = ast.parse(inspect.getsource(fn))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left, *node.comparators]
        if not any(isinstance(s, ast.Name) and s.id == "name" for s in sides):
            continue
        for s in sides:
            if isinstance(s, ast.Constant) and isinstance(s.value, str):
                found.add(s.value)
            elif isinstance(s, ast.Tuple):
                found.update(e.value for e in s.elts
                             if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return found


@check
def side_registries_dispatch_only_on_registered_names():
    # The three main tables are drift-proof by derivation, but these name-keyed surfaces can
    # still half-register a future region — police them by AST so no refactor is needed.
    names = set(NAMES)
    for fn in (regions_mod._locator_region, regions_mod._region_selected_by_source_needs,
               regions_mod._region_provenance):
        literals = _name_literals(fn)
        assert literals, f"{fn.__name__}: expected name-literal dispatch, found none (AST probe broke?)"
        assert literals <= names, f"{fn.__name__} dispatches on unregistered: {literals - names}"
    assert _SEALED_SOURCE_REGIONS <= names, _SEALED_SOURCE_REGIONS - names


@check
def golden_layout_snapshot():
    # Byte-for-byte render order is a pure function of (tuple order, slot, renderer output).
    # Adding/reordering/re-slotting a region must be a DELIBERATE diff to this literal.
    # 2026-08-03 Lane-B audit deletion (28 -> 20): closure/convergence (production-unreachable —
    # gating counters zeroed at every seal, seed built before any tool call), action_header/
    # action_history (render-dead: constant placeholder), evidence_result/evidence_detail/
    # quality_evidence_result/quality_evidence_detail (producer-dead: the mechanical admission
    # carries no evidence queries and make_evidence_snapshot returns None on the product path).
    assert tuple((r.name, r.tier, r.slot) for r in REGIONS) == (
        ("intent", STABLE, 0), ("task_objective", STABLE, 0), ("corrections", STABLE, 0),
        ("task_constraints", STABLE, 0), ("open_files", STABLE, 0), ("related_code", STABLE, 1),
        ("skills", STABLE, 2), ("memory", STABLE, 2),
        # 2026-08-04 Session Spine (docs/SESSION-SPINE-ROADMAP.md P4): frozen sealed-turn digests,
        # flag-gated (AGENT_SESSION_SPINE=1), renders "" otherwise.
        ("session_spine", STABLE, 2),
        # 2026-08-05 Session Tape (docs/SESSION-TAPE-DESIGN.md): the single append-only stream,
        # flag-gated (AGENT_SESSION_TAPE=1), renders "" otherwise; absorbs the spine when active.
        ("session_tape", STABLE, 2), ("conversation", STABLE, 2),
        ("findings", VOLATILE, 3), ("progress", VOLATILE, 3), ("world", VOLATILE, 3),
        ("threads", VOLATILE, 3), ("cache_manifest", VOLATILE, 3), ("turn_contract", VOLATILE, 6),
        ("focus", VOLATILE, 6), ("worktree", VOLATILE, 6), ("user_report", VOLATILE, 6),
        ("reconciliation", VOLATILE, 6), ("error", VOLATILE, 6),
    )


@check
def corrections_is_tier1_mandatory_user_authority():
    # The specific bug this registry kills: Tier-1 corrections fell through to
    # (50, TASK_STATE, DERIVED, False). Pin the intended values.
    priority, authority, freshness, mandatory = _REGION_META["corrections"]
    assert (priority, authority, freshness, mandatory) == (
        98, InstructionClass.USER, FreshnessClass.REVISION_BOUND, True)
    assert priority > _REGION_META["task_objective"][0]  # override wording outranks the base objective
    assert _REGION_ROLES["corrections"] is EpistemicRole.DIRECTIVE


@check
def mandatory_regions_render_lossless_only():
    # mandatory=True ⇒ no locator alternative (regions.py suppresses it) and the controller
    # must accept an all-mandatory selection without degradation surprises.
    from sliceagent_core.pfc import Slice
    from sliceagent_core.regions import build_context_blocks
    s = Slice(); s.reset("registry fixture task")
    s.last_error = "fixture error"; s.open_report = "fixture says it is broken"
    ctx = {"s": s, "artifacts": "(no open files)", "discovery": "", "memory": "",
           "threads": "", "max_findings": 8}
    blocks = build_context_blocks(ctx)
    mandatory_names = {r.name for r in REGIONS if r.mandatory}
    seen = set()
    for b in blocks:
        name = b.item_id.split(":", 1)[1] if ":" in b.item_id else b.item_id
        if name in mandatory_names:
            seen.add(name)
            assert b.fidelity is Fidelity.FULL and b.representation_loss is RepresentationLoss.NONE, b.item_id
    assert seen, "fixture rendered no mandatory region — fixture too empty to prove anything"
    ElasticityController().select(blocks)  # must not raise on the default capacity


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
