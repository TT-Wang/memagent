"""P5 byte-gate verdict — aggregates spine_probe runs and applies the PRE-REGISTERED criteria.

Registered 2026-08-04, before any p3/spine run completed (off r1 was in flight; its result
cannot inform these thresholds):
  GATE (all required, pooled cross-turn pairs across reps, n>=2 reps per arm):
    1. spine cross-turn survival median >= 80% absolute
    2. spine cross-turn median - p3 cross-turn median >= +10 percentage points
       (the "material delta" of the roadmap: head-freeze-alone wins are booked to P3, not the spine)
    3. spine same-turn median >= 96% (no within-turn regression)
    4. zero HARNESS INVALID rows (liveness holds in every run)
  Anything else = FAIL -> stop and diagnose; no quality runs on an unproven mechanism.
"""
from __future__ import annotations

import glob
import json
import os
import sys


def main(out_dir=None):
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "spine_probe_runs")
    runs = [json.load(open(p, encoding="utf-8")) for p in sorted(glob.glob(f"{out_dir}/*.json"))]
    if not runs:
        print("no runs found"); return 2
    arms: dict[str, list[dict]] = {}
    for r in runs:
        arms.setdefault(r["label"], []).append(r)

    def pooled(arm_runs, same_turn):
        fr = sorted(row["frac"] for r in arm_runs for row in r["rows"] if row["same_turn"] == same_turn)
        return (fr[len(fr) // 2] if fr else None), len(fr)

    print(f"{'arm':<7}{'reps':<6}{'cross-med':<11}{'same-med':<10}{'pairs x/s':<11}"
          f"{'fresh-tok':<11}{'invalid'}")
    stats = {}
    for label, arm_runs in sorted(arms.items()):
        xm, xn = pooled(arm_runs, False)
        sm, sn = pooled(arm_runs, True)
        fresh = [r["meter"].get("in_fresh") for r in arm_runs if r["meter"].get("in_fresh") is not None]
        inv = [r["invalid"] for r in arm_runs if r.get("invalid")]
        stats[label] = {"cross": xm, "same": sm, "invalid": inv,
                        "reps": len(arm_runs), "passed": [r["passed"] for r in arm_runs]}
        print(f"{label:<7}{len(arm_runs):<6}"
              f"{(f'{xm*100:.1f}%' if xm is not None else '—'):<11}"
              f"{(f'{sm*100:.1f}%' if sm is not None else '—'):<10}"
              f"{xn}/{sn:<9}"
              f"{(f'{sum(fresh)/len(fresh)/1000:.0f}k' if fresh else '—'):<11}"
              f"{len(inv)}")

    if "spine" not in stats or "p3" not in stats:
        print("\nverdict: INCOMPLETE — need both p3 and spine arms (n>=2 each)")
        return 2
    s, p = stats["spine"], stats["p3"]
    checks = [
        ("reps>=2 both arms", s["reps"] >= 2 and p["reps"] >= 2),
        ("no HARNESS INVALID", not s["invalid"] and not p["invalid"]),
        ("spine cross-turn >= 80%", (s["cross"] or 0) >= 0.80),
        ("delta vs p3 >= +10pp", (s["cross"] or 0) - (p["cross"] or 0) >= 0.10),
        ("spine same-turn >= 96%", (s["same"] or 0) >= 0.96),
    ]
    print()
    ok = True
    for name, good in checks:
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name}")
    print(f"\nverdict: {'BYTE GATE PASS' if ok else 'BYTE GATE FAIL — stop and diagnose'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
