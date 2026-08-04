"""Option B verdict — pre-registered criteria for the OPEN FILES locator experiment.

Registered 2026-08-05 BEFORE any run (docs/OPENFILES-SUBSUMPTION-DESIGN.md §5).
Arms by label prefix in evals/spine_probe_runs/: 'ctl' = AGENT_SESSION_SPINE=1 only,
'loc' = spine + AGENT_OPENFILES_LOCATORS=1. Ability gate is evaluated FIRST.

  1. ABILITY (gate): every run passed=True (scenario oracle + no abnormal turn stops — the
     bench folds both into `passed`). str_replace churn proxy: per-run mean str_replace calls
     per turn on 'loc' must be <= 1.5x the 'ctl' mean (a rising retry count is the mismatch
     signature the probe can see without tool-result bodies; exact mismatch-rate audit stays a
     manual log check before any graduation).
  2. LIVENESS (gate): on every 'loc' run, every turn that edits (str_replace/write_file/insert)
     also has read_file >= 1 that turn; and total read_file > 0. A no-read locator run is
     INVALID (dead affordance), never a win.
  3. BYTE (gate): 'loc' pooled cross-turn survival median >= 80%.
     ATTRIBUTION RULE (pre-registered): if gate 3 fails but the modal cross-turn break is NOT
     the OPEN FILES block (e.g. findings / conversation / intent family / spine tip), report
     MECHANISM PASS - GATE BLOCKED-BY-NEIGHBOR(<region>): the locator change did its job; the
     named neighbor becomes the next pre-registered arm. Only OPEN FILES-attributed breaks or
     an ability failure count against this design.
  4. n>=2 per arm per scenario before any verdict line is quotable.
Cost and same-turn survival are REPORTED (decision inputs), never gates here.
"""
from __future__ import annotations

import glob
import json
import os
import sys

EDIT_TOOLS = ("str_replace", "write_file", "insert", "create_file", "apply_patch")


def _runs(out_dir, prefix):
    rows = []
    for p in sorted(glob.glob(f"{out_dir}/*.json")):
        r = json.load(open(p, encoding="utf-8"))
        if r["label"].startswith(prefix):
            rows.append(r)
    return rows


def _pooled_median(arm, same_turn):
    fr = sorted(row["frac"] for r in arm for row in r["rows"] if row["same_turn"] == same_turn)
    return (fr[len(fr) // 2] if fr else None), len(fr)


def _mean_per_turn(arm, names):
    vals = []
    for r in arm:
        turns = r.get("tool_counts_per_turn") or []
        if turns:
            vals.append(sum(sum(t.get(n, 0) for n in names) for t in turns) / len(turns))
    return sum(vals) / len(vals) if vals else 0.0


def main(out_dir=None):
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "spine_probe_runs")
    ctl, loc = _runs(out_dir, "ctl"), _runs(out_dir, "loc")
    print(f"runs: ctl={len(ctl)} loc={len(loc)}")
    if not loc:
        print("verdict: INCOMPLETE — no treatment runs")
        return 2

    checks = []
    # 1. ability
    checks.append(("ABILITY: all runs passed", all(r["passed"] for r in ctl + loc)))
    ctl_sr, loc_sr = _mean_per_turn(ctl, ("str_replace",)), _mean_per_turn(loc, ("str_replace",))
    checks.append((f"ABILITY: str_replace churn loc {loc_sr:.1f}/turn <= 1.5x ctl {ctl_sr:.1f}/turn",
                   not ctl or loc_sr <= 1.5 * max(ctl_sr, 0.1)))
    # 2. liveness
    live_ok, reads_total = True, 0
    for r in loc:
        for t in r.get("tool_counts_per_turn") or []:
            reads_total += t.get("read_file", 0)
            if any(t.get(n, 0) for n in EDIT_TOOLS) and not t.get("read_file", 0):
                live_ok = False
    checks.append(("LIVENESS: read_file>=1 on every editing turn (loc)", live_ok))
    checks.append((f"LIVENESS: total read_file>0 (loc, saw {reads_total})", reads_total > 0))
    # 3. byte
    xm, xn = _pooled_median(loc, False)
    cm, _ = _pooled_median(ctl, False)
    sm, _ = _pooled_median(loc, True)
    print(f"loc cross-turn median {xm and xm*100:.1f}% (n={xn}) vs ctl {cm and cm*100:.1f}% · "
          f"loc same-turn {sm and sm*100:.1f}%")
    byte_ok = (xm or 0) >= 0.80
    checks.append(("BYTE: loc cross-turn >= 80%", byte_ok))
    # 4. n
    checks.append(("N: >=2 runs per arm", len(ctl) >= 2 and len(loc) >= 2))

    ok = True
    for name, good in checks:
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name}")
    if not byte_ok and loc:
        import collections
        brk = collections.Counter()
        for r in loc:
            brk.update(r.get("cross_turn_breaks") or {})
        top = brk.most_common(1)[0][0] if brk else "?"
        if "OPEN FILES" not in top:
            print(f"\n  ATTRIBUTION: modal break = {top} -> "
                  f"MECHANISM PASS - GATE BLOCKED-BY-NEIGHBOR({top})")
    print(f"\nverdict: {'OPTION B GATES PASS' if ok else 'GATES NOT PASSED — see rows above'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
