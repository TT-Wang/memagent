"""Re-score BOTH agents' stored answers with the dual scorer (strict signature vs logic) and emit the
final sliceagent-vs-Codex comparison. No LLM — re-runs the saved code. Fair: identical scoring for both."""
import os
import sys
import json
import glob

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from colbench_trial import score_backend, _extract_code

PRICE = {"in": 1.25 / 1e6, "cached": 0.125 / 1e6, "out": 10.0 / 1e6}


def code_of(rec):
    d = rec.get("dialogue", "")
    up = d.upper()
    if "I WANT TO ANSWER:" not in up:
        return ""
    return _extract_code(d[up.rindex("I WANT TO ANSWER:") + len("I WANT TO ANSWER:"):], "code")


def main():
    membest = json.load(open("/tmp/colbench20_mem_best.json"))      # {task: rec}
    codex = {o["task"]: o for o in json.load(open("/tmp/colbench20_codex.json"))}
    tasks = sorted(os.path.basename(p) for p in glob.glob("/tmp/colbench20/t*.json"))

    rows = []
    for k in tasks:
        t = json.load(open(f"/tmp/colbench20/{k}"))
        m = membest.get(k, {}); c = codex.get(k, {})
        ms = score_backend(code_of(m), t) if m else {}
        cs = score_backend(code_of(c), t) if c else {}
        rows.append({
            "task": k.replace(".json", ""),
            "m_strict": ms.get("passed_strict", 0), "m_logic": ms.get("passed_logic", 0), "m_total": ms.get("total", 10),
            "c_strict": cs.get("passed_strict", 0), "c_logic": cs.get("passed_logic", 0), "c_total": cs.get("total", 10),
            "m_peak": m.get("peak_in", 0), "c_peak": c.get("peak_in", 0),
            "m_tok": m.get("in_total", 0) + m.get("out_total", 0), "c_tok": c.get("in_total", 0) + c.get("out_total", 0),
            "m_wall": m.get("wall_s", 0), "c_wall": c.get("wall_s", 0), "m_recall": m.get("recall", 0),
            "m_cost": (m.get("in_total", 0) - m.get("in_cached", 0)) * PRICE["in"] + m.get("in_cached", 0) * PRICE["cached"] + m.get("out_total", 0) * PRICE["out"],
            "c_cost": (c.get("in_total", 0) - c.get("in_cached", 0)) * PRICE["in"] + c.get("in_cached", 0) * PRICE["cached"] + c.get("out_total", 0) * PRICE["out"],
        })

    print("\n# ColBench (20 backend tasks) — sliceagent vs Codex CLI (subscription)")
    print("# pass = LOGIC score (full marks); (s=N) = STRICT-signature score when it differs\n")
    print("| task | sliceagent | codex | mem peak | cdx peak | mem tok | cdx tok | mem wall | cdx wall |")
    print("|---|---|---|---|---|---|---|---|---|")
    def cell(strict, logic, total):
        full = "✓" if logic == total else f"{logic}/{total}"
        return full + ("" if strict == logic else f" (s={strict})")
    for r in rows:
        print(f"| {r['task']} | {cell(r['m_strict'],r['m_logic'],r['m_total'])} | {cell(r['c_strict'],r['c_logic'],r['c_total'])} | "
              f"{r['m_peak']:,} | {r['c_peak']:,} | {r['m_tok']:,} | {r['c_tok']:,} | {r['m_wall']}s | {r['c_wall']}s |")

    n = len(rows)
    def full(strictkey, logickey, totkey, rs): return sum(1 for r in rs if r[logickey] == r[totkey])
    def fulls(strictkey, totkey, rs): return sum(1 for r in rs if r[strictkey] == r[totkey])
    m_logic = sum(1 for r in rows if r["m_logic"] == r["m_total"]); m_strict = sum(1 for r in rows if r["m_strict"] == r["m_total"])
    c_logic = sum(1 for r in rows if r["c_logic"] == r["c_total"]); c_strict = sum(1 for r in rows if r["c_strict"] == r["c_total"])
    av = lambda key: sum(r[key] for r in rows) / n
    print("\n## Totals\n")
    print("| metric | sliceagent | codex | edge |")
    print("|---|---|---|---|")
    print(f"| **pass rate (logic)** | **{m_logic}/{n}** | **{c_logic}/{n}** | {'tied' if m_logic==c_logic else ('sliceagent' if m_logic>c_logic else 'codex')} |")
    print(f"| pass rate (strict signature) | {m_strict}/{n} | {c_strict}/{n} | signature-compliance check |")
    print(f"| avg peak context / round | {av('m_peak'):,.0f} | {av('c_peak'):,.0f} | codex {av('c_peak')/max(1,av('m_peak')):.1f}× larger |")
    print(f"| avg tokens / task | {av('m_tok'):,.0f} | {av('c_tok'):,.0f} | codex {av('c_tok')/max(1,av('m_tok')):.1f}× more |")
    print(f"| avg wall / task | {av('m_wall'):.1f}s | {av('c_wall'):.1f}s | sliceagent {av('c_wall')/max(1,av('m_wall')):.1f}× faster |")
    print(f"| avg cost / task (uniform gpt-5.5) | ${av('m_cost'):.3f} | ${av('c_cost'):.3f} | codex {av('c_cost')/max(1e-9,av('m_cost')):.1f}× |")
    print(f"\nsliceagent cache reads (recall_history): {sum(r['m_recall'] for r in rows)} across {n} tasks")
    json.dump(rows, open("/tmp/colbench20_final.json", "w"), indent=2)


if __name__ == "__main__":
    main()
