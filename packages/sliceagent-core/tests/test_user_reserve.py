"""Verbatim user reserve — both lanes, soft priority, bounded widening. No model/network.

Contract under test: the ring keeps its MAX_CONVERSATION floor and widens ONLY while the
cumulative pair chars fit USER_RESERVE_TOKENS (hard ceiling RESERVE_ROWS_CEILING, O(1));
reserved exchanges carry RESERVE_PRIORITY in BOTH lanes (adjacency blocks and the legacy
conversation region — path asymmetry is the known bug class); the reserve is SOFT: locator
alternatives survive, so ContextUnfit semantics are preserved."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent_core.context_compiler import _ADJACENCY_ROUNDS, _adjacency_blocks  # noqa: E402
from sliceagent_core.pfc import Slice  # noqa: E402
from sliceagent_core.regions import (MAX_CONVERSATION, RESERVE_PRIORITY, RESERVE_ROWS_CEILING,  # noqa: E402
                                     build_context_blocks, reserve_keep, user_reserve_chars)

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def _row(i, user="u", assistant="a", artifact=""):
    return {"user": f"{user}{i}", "assistant": f"{assistant}{i}", "artifact_id": artifact}


def _slice_with_ring(rows):
    s = Slice(); s.reset("reserve fixture")
    s.conversation = list(rows)
    return s


@check
def small_rows_widen_past_the_old_floor_up_to_the_ceiling():
    rows = [_row(i) for i in range(30)]
    keep = reserve_keep(rows, floor=MAX_CONVERSATION)
    assert keep == RESERVE_ROWS_CEILING, keep      # tiny rows: budget never binds, ceiling does
    assert keep > MAX_CONVERSATION                 # the mid-distance window is actually kept


@check
def budget_binds_beyond_the_floor():
    big = "x" * (user_reserve_chars() // 2 + 1)    # two rows overflow the budget
    rows = [{"user": big, "assistant": "", "artifact_id": ""} for _ in range(10)]
    keep = reserve_keep(rows, floor=MAX_CONVERSATION)
    assert keep == MAX_CONVERSATION, keep          # floor kept regardless; no budget extension


@check
def floor_rows_are_kept_even_when_over_budget():
    huge = "x" * (user_reserve_chars() * 2)
    rows = [{"user": huge, "assistant": "", "artifact_id": ""} for _ in range(6)]
    assert reserve_keep(rows, floor=MAX_CONVERSATION) == MAX_CONVERSATION
    # floor=0 answers "how many newest rows are RESERVED": none, they exceed the budget outright
    assert reserve_keep(rows, floor=0) == 0


@check
def ring_trim_in_record_user_is_budget_aware():
    from sliceagent_core.pfc import record_user
    s = Slice(); s.reset("t")
    for i in range(20):
        record_user(s, f"message {i}")
        s.conversation[-1]["assistant"] = f"reply {i}"   # complete the exchange
    assert len(s.conversation) == RESERVE_ROWS_CEILING, len(s.conversation)
    assert s.conversation[-1]["user"] == "message 19"    # newest kept, oldest dropped


@check
def adjacency_widens_and_marks_reserved_pairs():
    rows = [_row(i) for i in range(14)] + [{"user": "current", "assistant": "", "artifact_id": ""}]
    blocks = _adjacency_blocks(_slice_with_ring(rows))
    groups = {b.alternative_group for b in blocks}
    assert len(groups) > _ADJACENCY_ROUNDS, groups     # widened past the old 3
    fulls = sorted((b for b in blocks if b.block_id.endswith(":full")), key=lambda b: b.order)
    assert fulls and all(b.priority >= RESERVE_PRIORITY for b in fulls), \
        [(b.alternative_group, b.priority) for b in fulls]   # tiny pairs: all within budget
    # The band ascends with recency — oldest reserved still pages first (never savings-driven).
    assert [b.priority for b in fulls] == sorted(b.priority for b in fulls)
    assert fulls[-1].priority > fulls[0].priority


@check
def over_budget_older_pairs_keep_age_priority():
    half = "x" * (user_reserve_chars() // 2)
    rows = ([{"user": half, "assistant": half, "artifact_id": ""} for _ in range(4)]
            + [{"user": "current", "assistant": "", "artifact_id": ""}])
    blocks = _adjacency_blocks(_slice_with_ring(rows))
    fulls = sorted((b for b in blocks if b.block_id.endswith(":full")), key=lambda b: b.order)
    # floor keeps 3 pairs; only the NEWEST fits the pair-priced budget → 1 reserved, older keep 90-age
    priorities = [b.priority for b in fulls]
    assert priorities[-1] >= RESERVE_PRIORITY, priorities
    assert priorities[0] == 90 - (len(fulls) - 1), priorities


@check
def reserve_is_soft_locators_survive():
    rows = [_row(i, artifact=f"art{i}") for i in range(8)] + [
        {"user": "current", "assistant": "", "artifact_id": ""}]
    blocks = _adjacency_blocks(_slice_with_ring(rows))
    by_group: dict = {}
    for b in blocks:
        by_group.setdefault(b.alternative_group, []).append(b)
    assert all(len(v) == 2 for v in by_group.values()), \
        {k: len(v) for k, v in by_group.items()}   # full + locator: degradable, never hard-unfit


@check
def legacy_conversation_region_mirrors_the_reserve():
    s = _slice_with_ring([_row(i) for i in range(5)] + [
        {"user": "current", "assistant": "", "artifact_id": ""}])
    ctx = {"s": s, "artifacts": "(none)", "discovery": "", "memory": "", "threads": "",
           "max_findings": 8}
    blocks = build_context_blocks(ctx)
    conv = [b for b in blocks if b.item_id == "region:conversation" and b.block_id.endswith(":full")]
    assert conv and conv[0].priority == RESERVE_PRIORITY, conv and conv[0].priority
    # over-budget ring: normal priority, degrades like any region
    s2 = _slice_with_ring([{"user": "x" * user_reserve_chars(), "assistant": "y", "artifact_id": ""},
                           _row(1), {"user": "current", "assistant": "", "artifact_id": ""}])
    ctx2 = dict(ctx, s=s2)
    conv2 = [b for b in build_context_blocks(ctx2)
             if b.item_id == "region:conversation" and b.block_id.endswith(":full")]
    assert conv2 and conv2[0].priority != RESERVE_PRIORITY, conv2[0].priority


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
