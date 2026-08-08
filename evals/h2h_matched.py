"""h2h at MATCHED reasoning: sliceagent (bounded slice) vs Codex CLI (growing transcript), BOTH gpt-5.5
at the same reasoning effort (AGENT_REASONING / CODEX_EFFORT, default 'high'), on the 3 h2h scenarios.
Same scenarios, same fixed pre-written turns, same verifier. Metrics: pass · tokens (in/cached/out) ·
per-call peak_in · wall · steps.

Codex is a real transcript agent: turn 1 = `codex exec`, later turns = `codex exec resume <thread>`, so
its per-call input grows with the conversation — the contrast to sliceagent's re-sealed slice.

s2_largefile_bug plants its bug in a copy of CPython 3.13's argparse.py, so its setup() only runs under a
3.13 interpreter; build_workdir() falls back to ~/.sweb-venv (3.13) for that one (agents + verify still run
under the sliceagent 3.11 runtime — the workdir files are interpreter-independent once written).

Run:
  set -a; . "../agent design/.env"; set +a
  export LLM_API_KEY=$OPENAI_API_KEY AGENT_MODEL=gpt-5.5 AGENT_REASONING=high CODEX_EFFORT=high
  export https_proxy=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890 all_proxy=http://127.0.0.1:7890
  # codex uses subscription auth; DON'T unset OPENAI_API_KEY here — sliceagent needs LLM_API_KEY.
  # Instead codex ignores it because we pass its own model/effort; if codex bills API, that's fine (same model).
  PYTHONPATH=src .venv/bin/python -m evals.h2h_matched
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
sys.path.insert(0, os.path.join(REPO, "evals"))
from h2h_run import _UsageTap, load_scenario, run_sliceagent  # noqa: E402

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")
CODEX_EFFORT = os.environ.get("CODEX_EFFORT", "high")
TURN_TIMEOUT = int(os.environ.get("CODEX_TURN_TIMEOUT", "900"))
SWEB_PY = os.path.expanduser("~/.sweb-venv/bin/python")   # 3.13, for s2's argparse-3.13 setup
SCENARIOS = ["s1_longhorizon_debug", "s2_taskdag_scheduler", "s3_intervalset_algebra"]


def build_workdir(scn, prefix):
    """Fresh workdir with the scenario set up. In-process for most; for a version-fragile scenario whose
    setup() raises under 3.11 (s2 wants CPython 3.13 argparse) fall back to the 3.13 interpreter."""
    wd = tempfile.mkdtemp(prefix=prefix)
    try:
        scn["setup"](wd)
    except Exception as e:  # noqa: BLE001
        code = (f"import importlib.util,sys; "
                f"spec=importlib.util.spec_from_file_location('s', {scn['dir']!r}+'/setup.py'); "
                f"m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m.setup({wd!r})")
        r = subprocess.run([SWEB_PY, "-c", code], capture_output=True, text=True, cwd=REPO)
        if r.returncode != 0:
            raise RuntimeError(f"setup failed both in-proc ({e}) and via 3.13 ({r.stderr[-200:]})")
    return wd


def _codex_call(workdir, prompt, thread_id):
    # `codex exec resume` does NOT accept -C or --sandbox (only base exec does) → use cwd=workdir and the
    # `-c sandbox_mode=` config so BOTH the first exec and every resume run identically in the repo.
    common = ["--json", "--skip-git-repo-check", "-m", CODEX_MODEL,
              "-c", f'model_reasoning_effort="{CODEX_EFFORT}"', "-c", 'sandbox_mode="workspace-write"']
    cmd = ([CODEX_BIN, "exec", "resume", thread_id] + common + [prompt]) if thread_id \
        else ([CODEX_BIN, "exec"] + common + [prompt])
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=TURN_TIMEOUT, cwd=workdir)
    except subprocess.TimeoutExpired:
        return thread_id, {"peak_in": 0, "in": 0, "out": 0, "cached": 0}, "timeout", f"timeout {TURN_TIMEOUT}s"
    tin = tout = tcached = peak = 0
    for ln in proc.stdout.splitlines():
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("type") == "thread.started" and o.get("thread_id"):
            thread_id = o["thread_id"]
        elif o.get("type") == "turn.completed":
            u = o.get("usage", {}) or {}
            ti = u.get("input_tokens", 0)
            tin += ti; tout += u.get("output_tokens", 0); tcached += u.get("cached_input_tokens", 0)
            peak = max(peak, ti)
    return thread_id, {"peak_in": peak, "in": tin, "out": tout, "cached": tcached}, proc.returncode, \
        (proc.stderr.strip()[-160:] if proc.returncode not in (0,) else "")


def run_codex(scn, workdir):
    prompts = scn["prompts"]
    thread_id = None; per_turn = []; tin = tout = tcached = peak = 0; err = ""
    t0 = time.time()
    for i, p in enumerate(prompts):
        tt = time.time()
        thread_id, u, rc, e = _codex_call(workdir, p, thread_id)
        tin += u["in"]; tout += u["out"]; tcached += u["cached"]; peak = max(peak, u["peak_in"])
        per_turn.append({"turn": i + 1, "wall": round(time.time() - tt, 1), "peak_in": u["peak_in"], "in": u["in"], "rc": rc})
        if rc == "timeout":
            err = f"turn {i+1} {e}"; break
        if rc not in (0,) or thread_id is None:   # a broken/errored turn must NOT silently pass through
            err = f"turn {i+1}: codex rc={rc} thread={thread_id} {e}"; break
    wall = time.time() - t0
    passed, detail = (False, err) if err else scn["verify"](workdir)
    return {"agent": "codex", "scenario": scn["name"], "passed": bool(passed), "detail": str(detail)[:80],
            "steps": len(per_turn), "wall_s": round(wall, 1), "in_total": tin, "in_cached": tcached,
            "out_total": tout, "peak_in": peak, "per_turn": per_turn, "effort": CODEX_EFFORT}


def main():
    out = os.path.join(REPO, "evals", "colbench", "results", "h2h_matched.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    results = json.load(open(out)) if os.path.exists(out) else []
    for name in SCENARIOS:
        scn = load_scenario(name)
        for agent in ("sliceagent", "codex"):
            if any(r.get("scenario") == name and r.get("agent") == agent for r in results):
                print(f"skip {name}/{agent} (already done)", flush=True); continue
            wd = build_workdir(scn, f"h2h-{name}-{agent}-")
            print("=" * 80, flush=True); print(f"h2h MATCHED — {name} / {agent}", flush=True)
            if agent == "sliceagent":
                r = run_sliceagent(scn, wd, "gpt-5.5")   # AGENT_REASONING=high from env
            else:
                r = run_codex(scn, wd)
            r["workdir"] = wd
            results.append(r)
            json.dump(results, open(out, "w"), indent=2)
            print(f"  -> {'PASS' if r['passed'] else 'FAIL'} | steps={r['steps']} peak_in={r.get('peak_in',0):,} "
                  f"tok={r.get('in_total',0)+r.get('out_total',0):,} wall={r['wall_s']}s "
                  f"{r.get('detail','') if not r['passed'] else ''}", flush=True)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
