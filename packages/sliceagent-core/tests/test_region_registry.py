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
from sliceagent_core.context import (ContextBlock, ElasticityController, EpistemicRole,  # noqa: E402
                                     Fidelity, FreshnessClass, InstructionClass,
                                     RepresentationLoss)
from sliceagent_core.regions import (_REGION_META, _REGION_ROLES, _SEALED_SOURCE_REGIONS,  # noqa: E402
                                     REGION_ORDER, REGIONS, RegionSpec)

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
        assert isinstance(r.zone, int) and r.zone >= 0, (r.name, r.zone)
        assert callable(r.render), r.name
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
        name, zone, render, zone2 = entry
        assert (spec.name, spec.zone, spec.zone) == (name, zone, zone2)
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
    # _region_selected_by_source_needs lost its last name-literal dispatch when the conversation
    # region retired at tape graduation — it now routes purely on contract source_needs, so the
    # AST probe polices only the surfaces that still key on region names.
    for fn in (regions_mod._locator_region, regions_mod._region_provenance):
        literals = _name_literals(fn)
        assert literals, f"{fn.__name__}: expected name-literal dispatch, found none (AST probe broke?)"
        assert literals <= names, f"{fn.__name__} dispatches on unregistered: {literals - names}"
    stray = _name_literals(regions_mod._region_selected_by_source_needs) - names
    assert not stray, f"_region_selected_by_source_needs dispatches on unregistered: {stray}"
    # _graph_trim_selected keys on TABLES, not name-literal comparisons — police the tables
    # plus its inline selection tuples (kept in sync here; a rename fails this line).
    assert regions_mod._GRAPH_ALWAYS <= names, regions_mod._GRAPH_ALWAYS - names
    assert regions_mod._INTENT_FALLBACK <= names, regions_mod._INTENT_FALLBACK - names
    assert {"open_files", "worktree", "related_code", "skills", "findings"} <= names
    assert _SEALED_SOURCE_REGIONS <= names, _SEALED_SOURCE_REGIONS - names


@check
def one_placement_law():
    # "ONE AND ONLY SCHEMA" (2026-08-05): placement is a single declared field (RegionSpec.zone),
    # resolved by ONE function (region_zone), applied by ONE factory (context_block), enforced at
    # ONE seam (assert_placement_law). The legacy pair tier+slot and the side table _TAIL_SLOT are
    # gone — they were the same decision expressed three times and could drift apart.
    from sliceagent_core.regions import (assert_placement_law, context_block, region_zone,
                                         HEAD_ZONE, TAPE_ZONE, TAIL_ZONE, _NON_REGION_ZONES)
    import dataclasses as _dc
    fields = {f.name for f in _dc.fields(regions_mod.RegionSpec)}
    assert "zone" in fields, "the placement field must exist"
    assert not (fields & {"tier", "slot"}), f"legacy placement fields survive: {fields}"
    zones = {r.name: r.zone for r in REGIONS}
    assert [n for n, z in zones.items() if z == TAPE_ZONE] == ["session_tape"], "exactly ONE tape"
    assert [n for n, z in zones.items() if z == HEAD_ZONE] == [], \
        "the HEAD is empty by proof — nothing has been shown byte-frozen (skills was not)"
    # every registered name resolves, non-region producers are declared, unknowns fall to TAIL
    for r in REGIONS:
        assert region_zone(r.name) == r.zone and region_zone(f"region:{r.name}") == r.zone
    for item, z in _NON_REGION_ZONES.items():
        assert region_zone(item) == z >= TAIL_ZONE, item
    assert region_zone("a-region-invented-next-year") == TAIL_ZONE
    # the factory refuses a hand-picked slot, and derives the lawful one
    try:
        context_block("intent", block_id="x", alternative_group="x", priority=1,
                      instruction_class=InstructionClass.DATA, freshness=FreshnessClass.LIVE,
                      fidelity=Fidelity.FULL, representation_loss=RepresentationLoss.NONE,
                      content="c", slot=0)
        raise AssertionError("context_block must reject a caller-supplied slot")
    except TypeError:
        pass
    blk = context_block("intent", block_id="x", alternative_group="x", priority=1,
                        instruction_class=InstructionClass.DATA, freshness=FreshnessClass.LIVE,
                        fidelity=Fidelity.FULL, representation_loss=RepresentationLoss.NONE,
                        content="c")
    assert blk.slot == zones["intent"]
    # the seam rejects ANY producer above the tape, including one built by hand
    # BOTH seams, adversarially (review at 124dc13: the first validator trusted self-claimed
    # identity, so a forged `region:session_tape` at slot 0 walked through).
    kw = dict(alternative_group="g", priority=1, instruction_class=InstructionClass.DATA,
              freshness=FreshnessClass.LIVE, fidelity=Fidelity.FULL,
              representation_loss=RepresentationLoss.NONE, content="c")
    for item, slot in (("region:session_tape", 0), ("region:intent", 0), ("session_tape", 0),
                       ("future-producer", 0)):
        try:
            ContextBlock(block_id="x", item_id=item, slot=slot, **kw)
            raise AssertionError(f"constructor let {item} sit at zone {slot}")
        except ValueError:
            pass
    for item, slot in (("region:session_tape", 5), ("region:intent", 6)):
        blk = ContextBlock(block_id="x", item_id=item, slot=slot, **kw)
        try:
            assert_placement_law((blk,))
            raise AssertionError(f"seam let {item} sit at zone {slot}")
        except ValueError:
            pass
    tape2 = tuple(ContextBlock(block_id=b, item_id="region:session_tape", slot=TAPE_ZONE, **kw)
                  for b in ("a", "b"))
    try:
        assert_placement_law(tape2)
        raise AssertionError("two tape-zone blocks must be rejected")
    except ValueError:
        pass
    # the safe default: a producer that never thought about placement lands in the TAIL
    assert ContextBlock(block_id="x", item_id="future-producer", **kw).slot == TAIL_ZONE


@check
def the_factory_is_the_only_construction_site():
    """Structural, not advisory: product code must build blocks ONLY through context_block().
    An AST scan, because a comment cannot stop the next producer from bypassing the law."""
    import ast as _ast
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages")
    offenders = []
    for pkg in ("sliceagent-core", "sliceagent-cli"):
        base = os.path.join(root, pkg, "src")
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                tree = _ast.parse(open(path, encoding="utf-8").read())
                factory = next((n for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef)
                                and n.name == "context_block"), None)
                allowed = set(range(factory.lineno, factory.end_lineno + 1)) if factory else set()
                for n in _ast.walk(tree):
                    if (isinstance(n, _ast.Call) and getattr(n.func, "id", "") == "ContextBlock"
                            and n.lineno not in allowed):
                        offenders.append(f"{os.path.relpath(path, root)}:{n.lineno}")
    assert not offenders, (
        "ContextBlock built outside regions.context_block() — route it through the factory so "
        f"placement stays derived: {offenders}")


@check
def golden_layout_snapshot():
    # Byte-for-byte render order is a pure function of (tuple order, slot, renderer output).
    # Adding/reordering/re-slotting a region must be a DELIBERATE diff to this literal.
    # 2026-08-03 Lane-B audit deletion (28 -> 20): closure/convergence (production-unreachable —
    # gating counters zeroed at every seal, seed built before any tool call), action_header/
    # action_history (render-dead: constant placeholder), evidence_result/evidence_detail/
    # quality_evidence_result/quality_evidence_detail (producer-dead: the mechanical admission
    # carries no evidence queries and make_evidence_snapshot returns None on the product path).
    # Byte-for-byte render order is a pure function of (tuple order, ZONE, renderer output).
    # Adding/reordering/re-zoning a region must be a DELIBERATE diff to this literal.
    assert tuple((r.name, r.zone) for r in REGIONS) == (
        ("intent", 2), ("task_objective", 2), ("corrections", 2),
        ("task_constraints", 2), ("open_files", 2), ("related_code", 3),
        ("skills", 2), ("memory", 2), ("session_tape", 1),
        ("findings", 5), ("progress", 5), ("world", 5),
        ("threads", 5), ("turn_contract", 6), ("focus", 6),
        ("worktree", 6), ("user_report", 6), ("reconciliation", 6),
        ("error", 6),
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
