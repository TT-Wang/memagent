"""Aggregate the matched-reasoning benchmark results into the headline comparison.
Reads results/{sliceagent,codex}.json (ColBench N=20) and results/h2h_matched.json (3 h2h tasks),
prints per-agent pass-rate · tokens · peak_in · wall · cache% · cache-aware $ and the saving ratios.
gpt-5.5 pricing (cache-aware): $1.25/M fresh-in, $0.125/M cached-in, $10/M out.
Run: python3 evals/colbench/aggregate.py
"""
import json
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
R = os.path.join(HERE, "results")
IN_FRESH, IN_CACHED, OUT = 1.25 / 1e6, 0.125 / 1e6, 10.0 / 1e6


def _cost(in_total, in_cached, out_total):
    fresh = max(0, in_total - in_cached)
    return fresh * IN_FRESH + in_cached * IN_CACHED + out_total * OUT


def _load(name):
    p = os.path.join(R, name)
    return json.load(open(p)) if os.path.exists(p) else []


def summarize(rows, pass_is_full=True, denom_total=True):
    """pass_is_full: a task counts as passed only if passed==total (all 10 ColBench tests) OR passed==True (h2h)."""
    n = len(rows)
    def passed(r):
        if isinstance(r.get("passed"), bool):
            return r["passed"]
        return r.get("total") and r["passed"] == r["total"]
    npass = sum(1 for r in rows if passed(r))
    tin = sum(r.get("in_total", 0) for r in rows)
    # cache: h2h records carry in_cached directly; ColBench records carry cache_pct → derive in_cached.
    def _cached(r):
        if r.get("in_cached") is not None:
            return r["in_cached"]
        return round(r.get("in_total", 0) * r.get("cache_pct", 0) / 100)
    tcached = sum(_cached(r) for r in rows)
    tout = sum(r.get("out_total", 0) for r in rows)
    peaks = [r.get("peak_in", 0) for r in rows if r.get("peak_in")]
    walls = [r.get("wall_s", 0) for r in rows]
    return {
        "n": n, "pass": npass, "pass_pct": round(100 * npass / n) if n else 0,
        "in_total": tin, "in_cached": tcached, "out_total": tout, "tok_total": tin + tout,
        "peak_med": int(st.median(peaks)) if peaks else 0,
        "peak_mean": int(st.mean(peaks)) if peaks else 0,
        "wall_total": round(sum(walls), 1), "wall_med": round(st.median(walls), 1) if walls else 0,
        "cache_pct": round(100 * tcached / tin) if tin else 0,
        "cost": round(_cost(tin, tcached, tout), 4),
    }


def _pair(mem, cod, title):
    if not mem or not cod:
        print(f"\n## {title}: incomplete (sliceagent={len(mem)}, codex={len(cod)})"); return
    m, c = summarize(mem), summarize(cod)
    print(f"\n## {title}  (sliceagent N={m['n']} · codex N={c['n']})")
    hdr = f"{'metric':<22}{'sliceagent':>14}{'codex':>14}{'codex/slice':>13}"
    print(hdr); print("-" * len(hdr))
    def row(label, mk, ck, ratio_of=None, fmt="{:,}"):
        mv, cv = m[mk], c[ck]
        rr = f"{cv/mv:.2f}×" if (ratio_of and mv) else ""
        print(f"{label:<22}{fmt.format(mv):>14}{fmt.format(cv):>14}{rr:>13}")
    print(f"{'fully passed':<22}{m['pass']:>2}/{m['n']:<11}{c['pass']:>2}/{c['n']:<11}")
    row("peak input (median)", "peak_med", "peak_med", ratio_of=True)
    row("peak input (mean)", "peak_mean", "peak_mean", ratio_of=True)
    row("total tokens", "tok_total", "tok_total", ratio_of=True)
    row("output tokens", "out_total", "out_total", ratio_of=True)
    row("wall total (s)", "wall_total", "wall_total", ratio_of=True, fmt="{}")
    row("wall median (s)", "wall_med", "wall_med", ratio_of=True, fmt="{}")
    print(f"{'cache %':<22}{m['cache_pct']:>13}%{c['cache_pct']:>13}%")
    row("cost $ (cache-aware)", "cost", "cost", ratio_of=True, fmt="${}")


def main():
    _pair(_load("sliceagent.json"), _load("codex.json"), "ColBench backend (multi-turn, gpt-5.5 high)")
    h = _load("h2h_matched.json")
    _pair([r for r in h if r["agent"] == "sliceagent"], [r for r in h if r["agent"] == "codex"],
          "h2h scenarios (multi-turn/step, gpt-5.5 high)")
    # per-task ColBench table
    mem = {r["task"]: r for r in _load("sliceagent.json")}
    cod = {r["task"]: r for r in _load("codex.json")}
    if mem and cod:
        print("\n## per-task ColBench (pass/10 · peak_in · tok · wall)")
        print(f"{'task':<8}{'slice pass':>11}{'cdx pass':>10}{'slice peak':>12}{'cdx peak':>10}"
              f"{'slice tok':>11}{'cdx tok':>10}{'slice wall':>11}{'cdx wall':>10}")
        for k in sorted(mem):
            m, c = mem[k], cod.get(k, {})
            print(f"{k.replace('.json',''):<8}{m['passed']}/{m['total']:<8}{c.get('passed','?')}/{c.get('total','?')!s:<7}"
                  f"{m['peak_in']:>12,}{c.get('peak_in',0):>10,}{m['in_total']+m['out_total']:>11,}"
                  f"{c.get('in_total',0)+c.get('out_total',0):>10,}{m['wall_s']:>10}s{str(c.get('wall_s','?'))+'s':>10}")


if __name__ == "__main__":
    main()
