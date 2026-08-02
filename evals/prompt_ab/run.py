"""Driver for the scientific prompt A/B suite — two-stage screen→confirm, paired same-batch control.

Each CELL = (variant, metric, trial) → a subprocess running evals.prompt_ab.metrics with the variant's
prompt injected (SLICEAGENT_PROMPT_FILE) and the stage's provider env. Cells run concurrently (--jobs).
Aggregation is PAIRED by item: per variant we average each item over its trials, then compare the variant's
per-item vector to the control's with a bootstrap CI of the paired difference (stats.py). The control runs
in the SAME batch as every variant (never a remembered baseline). The dedupe NEGATIVE CONTROL re-confirms
the FP-doubling baseline each batch — if it doesn't regress, the harness/judge drifted and the run is suspect.

Providers (keys sourced from your env / .env): deepseek (CN-direct, cheap) screens; gpt5 confirms.
  PLAN (no spend):  PYTHONPATH=src:evals .venv/bin/python -m evals.prompt_ab.run --plan
  RUN:              set -a; source "/Users/tongtao/Desktop/agent design/.env"; set +a
                    PYTHONPATH=src:evals .venv/bin/python -m evals.prompt_ab.run --stage both
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import stats as _stats          # noqa: E402
import variants as _variants    # noqa: E402

# provider -> (key env var, base_url, model, judge_model)
PROVIDERS = {
    # deepseek-chat/-reasoner were RETIRED 2026-07-25 (the alias may still resolve today, but that is
    # provider grace, not a contract) — pin the current names.
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "deepseek-v4-flash", "deepseek-v4-flash"),
    "deepseekpro": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "deepseek-v4-pro", "deepseek-v4-pro"),
    "gpt5": ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-5.5", "gpt-5.5"),
    "moonshot": ("MOONSHOT_API_KEY", "https://api.moonshot.cn/v1", "kimi-k2.7-code", "kimi-k2.7-code"),
}
# the metric each variant primarily TARGETS — used for screen promotion + the headline read.
TARGET_METRIC = {
    "v01_recency_verify": ("review", "false_pos"),
    "v02_lead_verification": ("review", "false_pos"),
    "v03_recency_actconverse": ("convo", "intent_ok"),
    "v04_precedence_tag": ("review", "false_pos"),
    "ctrl_dedupe": ("review", "false_pos"),
}
LOWER_BETTER = {("review", "false_pos"), ("convo", "length_chars")}

# COMPONENT flag-arms (convergence spec P2): an arm is an ENV DICT, not a prompt transform. The
# prompt seam stays UNSET for flag-arms (both arms run the shipped prompt); the component gates on
# sliceagent_core.flags and reads its AGENT_EXPERIMENTAL_<ID> env live. ctrl_dedupe (a prompt
# transform) is meaningless here and is excluded automatically when --flag-arm is used.
FLAG_ARMS = {
    "intent_mechanical": {"AGENT_EXPERIMENTAL_INTENT_MECHANICAL": "1"},
    "overflow_simple": {"AGENT_EXPERIMENTAL_OVERFLOW_SIMPLE": "1"},
}
FLAG_ARM_TARGET = {   # headline metric per component arm (screen promotion + the deletion read)
    "intent_mechanical": ("review", "false_pos"),
    "overflow_simple": ("tasks", "passed"),
}
_FLAG_MODE = False    # set by --flag-arm: control cells run the SHIPPED prompt (no prompt seam)


def _preflight_flag_arm(arm: str) -> None:
    """R4: flags resolve unknown ids to False — a typo'd env name silently runs control-vs-control.
    Prove IN A SUBPROCESS (the cell isolation the real run uses) that the arm env actually flips
    the registered flag before any money is spent."""
    import subprocess
    flag_id = arm
    # Import the REGISTERING modules (pfc/loop), matching a real cell's import topology: enabled()
    # deliberately resolves unregistered ids False, so checking flags.py alone would itself be an
    # A/A — this preflight's very first run caught exactly that.
    code = (f"import sys; sys.path.insert(0, {os.path.join(ROOT, 'src')!r}); "
            f"import sliceagent.pfc, sliceagent.loop; "
            f"from sliceagent.flags import enabled; print(int(enabled({flag_id!r})))")
    off = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    on_env = {**os.environ, **FLAG_ARMS[arm], "PYTHONDONTWRITEBYTECODE": "1"}
    on = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=on_env)
    if off.stdout.strip() != "0" or on.stdout.strip() != "1":
        raise SystemExit(
            f"preflight FAILED for flag-arm {arm!r}: control={off.stdout.strip()!r} "
            f"arm={on.stdout.strip()!r} (expected 0/1). A silent A/A run was prevented. "
            f"stderr: {(on.stderr or off.stderr)[-300:]}")
SCREEN_METRICS = ["review", "convo"]              # tasks is confirm-only (expensive, multi-turn)
CONFIRM_METRICS = ["review", "convo", "tasks"]
PER_TRIAL_DESC = {"review": "3 targets x (review run + judge)", "convo": "6 cases x (run + judge)",
                  "tasks": "3 staged multi-turn scenarios"}


def _cell_env(variant, provider):
    keyvar, base, model, judge = PROVIDERS[provider]
    env = dict(os.environ)
    if variant in FLAG_ARMS:
        env.update(FLAG_ARMS[variant])            # component arm: flag env, shipped prompt
    elif not _FLAG_MODE:
        env["SLICEAGENT_PROMPT_FILE"] = _variants.variant_path(variant)
    # (flag-mode control: no prompt seam either — BOTH arms run the shipped prompt; the flag env
    # is the single differing variable, which is the whole point of a component A/B.)
    env["LLM_API_KEY"] = os.environ.get(keyvar, "")
    env["LLM_BASE_URL"] = base
    env["AGENT_MODEL"] = model
    if provider != "gpt5":
        env["AGENT_PROXY"] = "off"
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + os.path.join(ROOT, "evals")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env, model, judge


def _run_cell(variant, metric, trial, provider):
    env, model, judge = _cell_env(variant, provider)
    out = tempfile.mktemp(suffix=f"_{variant}_{metric}_{trial}.json")
    cmd = [sys.executable, "-m", "evals.prompt_ab.metrics", "--metric", metric,
           "--model", model, "--judge-model", judge, "--out", out]
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=2400)
        if not os.path.exists(out):
            return {"variant": variant, "metric": metric, "trial": trial, "items": [],
                    "error": (p.stderr or p.stdout or "no output")[-300:]}
        data = json.load(open(out))
        os.remove(out)
        return {"variant": variant, "metric": metric, "trial": trial, "items": data["items"]}
    except subprocess.TimeoutExpired:
        return {"variant": variant, "metric": metric, "trial": trial, "items": [], "error": "timeout"}


def _aggregate(cells):
    """{variant: {(metric, scorekey): {item: mean-over-trials}}}."""
    acc = {}
    for c in cells:
        for it in c["items"]:
            item = it["item"]
            for k, val in it.items():
                if k in ("item", "error") or not isinstance(val, (int, float)):
                    continue
                acc.setdefault(c["variant"], {}).setdefault((c["metric"], k), {}) \
                   .setdefault(item, []).append(float(val))
    means = {}
    for v, d in acc.items():
        for mk, items in d.items():
            for item, vals in items.items():
                means.setdefault(v, {}).setdefault(mk, {})[item] = sum(vals) / len(vals)
    return means


def _leaderboard(means):
    ctrl = means.get("control", {})
    scorekeys = sorted({mk for v in means.values() for mk in v})
    rows = []
    for mk in scorekeys:
        c_items = ctrl.get(mk, {})
        for v in means:
            if v == "control":
                continue
            v_items = means[v].get(mk, {})
            common = sorted(set(c_items) & set(v_items))
            if not common:
                continue
            cv = [c_items[i] for i in common]
            vv = [v_items[i] for i in common]
            rows.append((mk, v, _stats.mean(vv), _stats.mean(cv), _stats.paired_diff(cv, vv)))
    return rows


def _fmt(rows):
    out = [f"{'metric.score':22} {'variant':24} {'var':>8} {'ctrl':>8} {'Δ(v-c)':>9} {'95% CI':>20} sig"]
    for (mk, v, vm, cm, d) in sorted(rows, key=lambda r: (r[0], r[1])):
        lbl = f"{mk[0]}.{mk[1]}" + ("↓" if mk in LOWER_BETTER else "")
        out.append(f"{lbl:22} {v:24} {vm:8.3f} {cm:8.3f} {d['diff']:+9.3f} "
                   f"[{d['lo']:+.3f},{d['hi']:+.3f}]".ljust(72) + ("  ***" if d["significant"] else ""))
    return "\n".join(out)


def _survivors(chosen, means):
    """Keep control + any variant that does NOT significantly regress its OWN target metric."""
    keep = ["control"]
    for v in chosen:
        if v == "control":
            continue
        tgt = TARGET_METRIC.get(v)
        ok = True
        if tgt and means.get(v, {}).get(tgt) and means.get("control", {}).get(tgt):
            common = sorted(set(means[v][tgt]) & set(means["control"][tgt]))
            if common:
                cv = [means["control"][tgt][i] for i in common]
                vv = [means[v][tgt][i] for i in common]
                d = _stats.paired_diff(cv, vv)
                worse = (d["diff"] > 0) if tgt in LOWER_BETTER else (d["diff"] < 0)
                ok = not (worse and d["significant"])
        if ok:
            keep.append(v)
        else:
            print(f"  screen drops {v} (significantly regressed {tgt[0]}.{tgt[1]})")
    return keep


def _run_stage(stage, provider, metrics, n, variants_, jobs, out_dir):
    keyvar = PROVIDERS[provider][0]
    if not os.environ.get(keyvar):
        print(f"\n[{stage}] SKIP — {keyvar} not in env (source your .env first)")
        return [], {}
    jobspec = [(v, m, t) for v in variants_ for m in metrics for t in range(n)]
    # COUNTERBALANCE ORDER: shuffle (fixed seed) so each arm spreads across the whole run instead of running
    # in a block — otherwise arm is confounded with wall-clock/API drift (the first screen showed every arm
    # landing at the same FP because control ran first). Decorrelates arm from time; reproducible.
    random.Random(20260630).shuffle(jobspec)
    print(f"\n=== {stage.upper()} on {provider} ({PROVIDERS[provider][2]}): "
          f"{len(jobspec)} cells, jobs={jobs} (order-counterbalanced) ===")
    results = []
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(_run_cell, v, m, t, provider): (v, m, t) for (v, m, t) in jobspec}
        done = 0
        for f in as_completed(futs):
            r = f.result()
            results.append(r)
            done += 1
            err = f"  ERR {r['error'][:70]}" if r.get("error") else ""
            print(f"  [{done}/{len(jobspec)}] {r['variant']}/{r['metric']}#{r['trial']} "
                  f"{len(r['items'])} items{err}")
    means = _aggregate(results)
    print("\n" + _fmt(_leaderboard(means)))
    json.dump(results, open(os.path.join(out_dir, f"{stage}_{provider}_raw.json"), "w"), indent=2)
    return results, means


def _plan(chosen, a, screen_metrics, confirm_metrics):
    print("PROMPT A/B — PLAN (no spend)\nvariants:", ", ".join(chosen))
    total = 0
    for stage, prov, mets, n in [("screen", a.screen_provider, screen_metrics, a.screen_n),
                                 ("confirm", a.confirm_provider, confirm_metrics, a.confirm_n)]:
        cells = len(chosen) * len(mets) * n
        total += cells
        print(f"\n[{stage}] provider={prov}  variants={len(chosen)}  n={n}  metrics={mets}  → {cells} cells")
        for m in mets:
            print(f"    {m:7} {len(chosen) * n:3d} trials x {PER_TRIAL_DESC[m]}")
    print(f"\nTOTAL cells if all promoted: {total}. Trim with --variants / --screen-n / --confirm-n.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["screen", "confirm", "both"], default="both")
    ap.add_argument("--variants", default="all", help="'all' or comma list (control always included)")
    ap.add_argument("--screen-provider", default="deepseek")
    ap.add_argument("--confirm-provider", default="gpt5")
    ap.add_argument("--screen-n", type=int, default=3)
    ap.add_argument("--confirm-n", type=int, default=8)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--metrics", default="", help="override stage metrics for both stages (e.g. 'convo')")
    ap.add_argument("--flag-arm", default="", help=f"component A/B: control vs one of {sorted(FLAG_ARMS)}")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--out-dir", default=os.path.join(HERE, "runs"))
    a = ap.parse_args()
    override = a.metrics.split(",") if a.metrics else None
    screen_metrics = override or SCREEN_METRICS
    confirm_metrics = override or CONFIRM_METRICS

    if a.flag_arm:
        if a.flag_arm not in FLAG_ARMS:
            raise SystemExit(f"unknown flag arm {a.flag_arm!r}; known: {sorted(FLAG_ARMS)}")
        _preflight_flag_arm(a.flag_arm)           # R4: never spend on a silent A/A
        global _FLAG_MODE
        _FLAG_MODE = True
        TARGET_METRIC[a.flag_arm] = FLAG_ARM_TARGET[a.flag_arm]
        chosen = ["control", a.flag_arm]          # no ctrl_dedupe: it is a prompt transform
    else:
        names = _variants.build_all()
        if a.variants == "all":
            chosen = names
        else:
            want = set(a.variants.split(","))
            chosen = [n for n in names if n == "control" or n in want]
            if "control" not in chosen:
                chosen = ["control"] + chosen

    if a.plan:
        _plan(chosen, a, screen_metrics, confirm_metrics)
        return 0

    os.makedirs(a.out_dir, exist_ok=True)
    allcells = []
    survivors = chosen
    if a.stage in ("screen", "both"):
        sc, means = _run_stage("screen", a.screen_provider, screen_metrics, a.screen_n,
                               chosen, a.jobs, a.out_dir)
        allcells += sc
        if means:
            survivors = _survivors(chosen, means)
            print("\nsurvivors →", ", ".join(survivors))
    if a.stage in ("confirm", "both"):
        cf, _ = _run_stage("confirm", a.confirm_provider, confirm_metrics, a.confirm_n,
                           survivors, a.jobs, a.out_dir)
        allcells += cf
    json.dump(allcells, open(os.path.join(a.out_dir, "all_cells.json"), "w"), indent=2)
    print(f"\nraw cells → {os.path.join(a.out_dir, 'all_cells.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
