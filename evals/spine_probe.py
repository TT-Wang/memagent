"""P5 byte gate — instrumented prefix-survival probe (docs/SESSION-SPINE-ROADMAP.md P5).

Runs ONE scripted benchmark scenario under the current AGENT_SESSION_SPINE config and measures,
per consecutive model-call pair, how much of the serialized request survives as a common prefix
(DeepSeek bills everything after the first changed byte). Same-turn pairs measure within-turn
stability; CROSS-TURN pairs measure the rebuild — the number the spine exists to fix.

Pre-registered gate (roadmap P5, written before any run):
  - baseline = the P3-ONLY config (AGENT_SESSION_SPINE=p3: head stability, no spine region),
    re-measured because the 39-44% figure predates P3
  - spine-on passes only if (a) cross-turn survival >= 80% absolute AND (b) a material delta
    over the P3-only baseline; same-turn >= 96% maintained
  - liveness is asserted per arm (a spine arm whose requests never contain the region is
    HARNESS INVALID, not a low score)

Attribution v2: divergence is mapped to the nearest header ABOVE the break — both single-hash
region headers ("# SESSION SPINE", "# OPEN FILES", ...) and file headers ("### path") — the
single-hash recognition is the P5 fix; the old probe only knew "### " and filed every region
break as "pre-headers".

Usage:
  AGENT_SESSION_SPINE=1 .venv/bin/python evals/spine_probe.py --scenario s2_taskdag_scheduler \
      --label spine --out evals/spine_probe_runs
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "benchmarks"))
sys.path.insert(0, os.path.join(ROOT, "src"))

_spec = importlib.util.spec_from_file_location("bench_run", os.path.join(ROOT, "benchmarks", "run.py"))
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

CAP: dict = {"calls": []}


class _ProbeTap(bench._Tap):
    def _capture(self, messages):
        CAP["calls"].append({
            "messages_ser": json.dumps(messages, ensure_ascii=False, sort_keys=False),
            "n_msgs": len(messages),
        })

    def complete(self, messages, tools):
        self._capture(messages)
        return super().complete(messages, tools)

    def complete_with_control(self, messages, schemas, **kw):
        self._capture(messages)
        return super().complete_with_control(messages, schemas, **kw)


def common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def region_at(ser: str, off: int) -> str:
    """Nearest header at/above the divergence offset in the JSON-serialized request.

    Recognises BOTH header shapes (the v2 fix): region headers are single-hash lines
    ('\\n# NAME (...)' — appearing escaped as '\\\\n# ' in the serialization) and file headers
    inside OPEN FILES are triple-hash ('### path'). The NEARER one above the break wins, so a
    break inside a file block still names the file while a break at a region boundary names
    the region instead of dissolving into 'pre-headers'."""
    upto = ser[:off]
    msg_idx = max(upto.count('"role"') - 1, 0)
    region_hdr = upto.rfind("\\n# ")
    file_hdr = upto.rfind("### ")
    hdr = max(region_hdr, file_hdr)
    if hdr < 0:
        return f"msg{msg_idx}:pre-headers"
    if hdr == region_hdr:
        start = hdr + len("\\n# ")
        tag = "region"
    else:
        start = hdr + len("### ")
        tag = "file"
    end = len(ser)
    for stop in ("\\n", " (", "\\u"):
        cut = ser.find(stop, start)
        if 0 < cut < end:
            end = cut
    name = ser[start:min(end, start + 44)].strip()
    return f"msg{msg_idx}:{tag}:{name[:36]}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="s2_taskdag_scheduler")
    ap.add_argument("--label", required=True, help="config label recorded in the output (off/p3/spine)")
    ap.add_argument("--out", default=os.path.join(ROOT, "evals", "spine_probe_runs"))
    ap.add_argument("--rep", default="1")
    args = ap.parse_args(argv)

    flag = os.environ.get("AGENT_SESSION_SPINE", "").strip()
    bench._Tap = _ProbeTap
    scn = bench.load_scenario(args.scenario)
    t0 = time.time()
    res = bench.run(scn, memory_mode="real")

    calls = CAP["calls"]
    turn_of = []
    for t in res["per_turn"]:
        turn_of += [t["turn"]] * t["calls"]

    rows = []
    for i in range(1, len(calls)):
        a, b = calls[i - 1]["messages_ser"], calls[i]["messages_ser"]
        cp = common_prefix_len(a, b)
        same_turn = turn_of[i - 1] == turn_of[i] if i < len(turn_of) else True
        # WITHIN-turn the correct metric is APPEND INTEGRITY, not a survival ratio: b should
        # EXTEND a, and whole-list JSON serialization guarantees cp == len(a)-1 for a perfect
        # append (a's closing ']' becomes ',').  A frac<1 on a growing request is arithmetic,
        # not churn — misreading that cost this program a false within-turn diagnosis (erratum
        # in SESSION-SPINE-P5-VERDICT.md).  Cross-turn pairs are real rebuilds; frac stands.
        rows.append({"pair": i, "same_turn": same_turn, "cp_chars": cp, "next_len": len(b),
                     "frac": cp / max(len(b), 1), "break_at": region_at(b, cp),
                     "append_gap": (len(a) - cp) if same_turn else None})

    # ---- tail composition (decision data for the ratio ceiling): per-region char sizes of the
    # LAST turn's first-call request, split on serialized single-hash region headers. Which bytes
    # dominate below the spine is what arbitrates "layout done, tail is the remaining problem".
    composition = {}
    first_of_last_turn = None
    for i in range(1, len(turn_of)):
        if turn_of[i] != turn_of[i - 1]:
            first_of_last_turn = i
    if first_of_last_turn is not None:
        ser = calls[first_of_last_turn]["messages_ser"]
        marks = []
        pos = 0
        while True:
            hit = ser.find("\\n# ", pos)
            if hit < 0:
                break
            end = min(x for x in (ser.find("\\n", hit + 4), ser.find(" (", hit + 4), hit + 44)
                      if x > 0)
            marks.append((hit, ser[hit + 4:end].strip()[:36]))
            pos = hit + 4
        marks.append((len(ser), "END"))
        composition = {"_pre_first_header": marks[0][0] if marks else len(ser)}
        for (start, name), (nxt, _n2) in zip(marks, marks[1:]):
            composition[name] = composition.get(name, 0) + (nxt - start)

    # ---- per-turn tool-call counts (Option B liveness: read_file must be >0 on turns that edit;
    # parsed from the LAST request's trajectory, never string-matched — locator lines themselves
    # contain 'read_file(' and would false-positive a substring count)
    tool_counts = []
    last_of_turn = {}
    for i in range(len(turn_of)):
        last_of_turn[turn_of[i]] = i
    for turn, idx in sorted(last_of_turn.items()):
        counts = {}
        try:
            for msg in json.loads(calls[idx]["messages_ser"]):
                for tc in (msg.get("tool_calls") or []) if isinstance(msg, dict) else []:
                    name = str(((tc.get("function") or {}) if isinstance(tc, dict) else {}).get("name", ""))
                    if name:
                        counts[name] = counts.get(name, 0) + 1
        except Exception:  # noqa: BLE001 — a torn capture must not kill the whole report
            counts = {"_parse_error": 1}
        tool_counts.append({"turn": turn, **counts})

    # ---- liveness (mandatory; a dead arm is HARNESS INVALID, not a low score)
    spine_seen = sum(1 for c in calls if "# SESSION SPINE" in c["messages_ser"])
    late_calls = sum(t["calls"] for t in res["per_turn"][1:])   # calls after turn 1
    liveness = {
        "flag": flag, "spine_blocks_seen": spine_seen, "calls": len(calls),
        "calls_after_turn1": late_calls,
        "episodes_written": res.get("episodes_written"),
    }
    tape_seen = sum(1 for c in calls if "# SESSION TAPE" in c["messages_ser"])
    liveness["tape_blocks_seen"] = tape_seen
    for k in ("tape_entries", "tape_drift", "tape_rebased"):
        if k in res:
            liveness[k] = res[k]
    invalid = ""
    if args.label == "spine" and late_calls and spine_seen == 0:
        invalid = "HARNESS INVALID: spine arm but no SESSION SPINE block in any request"
    if args.label in ("off", "p3") and spine_seen:
        invalid = f"HARNESS INVALID: {args.label} arm but SESSION SPINE rendered {spine_seen}x"
    if args.label == "tape" and late_calls and tape_seen == 0:
        invalid = "HARNESS INVALID: tape arm but no SESSION TAPE block in any request"

    def stats(kind):
        sel = sorted(r["frac"] for r in rows if r["same_turn"] == kind)
        if not sel:
            return {"n": 0}
        return {"n": len(sel), "median": sel[len(sel) // 2], "min": sel[0],
                "mean": sum(sel) / len(sel)}

    breaks = collections.Counter(r["break_at"] for r in rows if not r["same_turn"])
    summary = {
        "label": args.label, "scenario": args.scenario, "rep": args.rep,
        "passed": res["passed"], "detail": res["detail"], "wall_s": round(time.time() - t0, 1),
        "same_turn": stats(True), "cross_turn": stats(False),
        "cross_turn_breaks": dict(breaks.most_common(10)),
        "meter": {k: res.get(k) for k in ("in_total", "in_cached", "in_fresh", "out_total",
                                          "peak_in", "cost_usd", "calls")},
        "liveness": liveness, "invalid": invalid,
        "composition_last_turn_first_call": composition,
        "tool_counts_per_turn": tool_counts,
        "same_turn_append_integrity": {
            "clean": sum(1 for row in rows
                         if row["same_turn"] and (row["append_gap"] or 0) <= 1),
            "n": sum(1 for row in rows if row["same_turn"] and row["append_gap"] is not None),
        },
        "rows": rows,
    }
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"{args.label}-{args.scenario}-r{args.rep}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False)

    ct = summary["cross_turn"]
    ai = summary["same_turn_append_integrity"]
    print(f"[{args.label} r{args.rep}] passed={res['passed']} "
          f"cross-turn median {ct.get('median', 0) * 100:.1f}% (n={ct['n']}) · "
          f"same-turn append-integrity {ai['clean']}/{ai['n']}"
          + (f"  !! {invalid}" if invalid else ""))
    for name, cnt in breaks.most_common(5):
        print(f"    cross-turn breaks at {name:<48} x{cnt}")
    print(f"wrote {path}")
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(main())
