#!/usr/bin/env python3
"""Kimi Code as a SECOND transcript control arm on the multi-turn coding scenarios.

Same fixed pre-written turns as benchmarks/run.py (sliceagent) and benchmarks/mini_arm.py
(mini-swe-agent); the model is pinned per-invocation with `-m` (e.g. deepseek/v4-flash, a custom
provider entry in ~/.kimi-code/config.toml), so all three arms can run the SAME provider model and
differ only in agent architecture. Kimi is a production transcript agent — session resume
(`kimi -r <session>`) carries the full conversation forward, and unlike mini it ships its own
context management, so it is the MITIGATED-transcript data point between mini's raw accumulation
and sliceagent's bounded slice.

Multi-turn driving and usage extraction follow evals/h2h_run.py (the proven Kimi h2h driver):
turn 1 `kimi -p`, later turns `kimi -r <session> -p`, usage read back from the session's
wire.jsonl usage.record rows — the CLI prints no usage. Per-turn attribution = the delta in the
usage-record list between turn boundaries.

Usage:
  python benchmarks/kimi_arm.py --scenario s1_longhorizon_debug --model deepseek/v4-flash \
      --out /tmp/kimi_arm_results
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import subprocess
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TASKS = os.path.join(HERE, "multiturn_coding")
KIMI_BIN = os.path.expanduser("~/.kimi-code/bin/kimi")
KIMI_HOME = os.path.expanduser("~/.kimi-code")
TURN_TIMEOUT = int(os.environ.get("KIMI_TURN_TIMEOUT", "900"))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_scenario(name):
    d = os.path.join(TASKS, name)
    return {
        "name": name,
        "meta": json.load(open(os.path.join(d, "meta.json"))),
        "prompts": json.load(open(os.path.join(d, "prompts.json"))),
        "setup": _load(os.path.join(d, "setup.py"), f"{name}_setup").setup,
        "verify": _load(os.path.join(d, "verify.py"), f"{name}_verify").verify,
    }


def _session_dir(session_id):
    idx = os.path.join(KIMI_HOME, "session_index.jsonl")
    if os.path.exists(idx):
        for ln in open(idx):
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if o.get("sessionId") == session_id and os.path.isdir(o.get("sessionDir", "")):
                return o["sessionDir"]
    hits = glob.glob(os.path.join(KIMI_HOME, "sessions", "*", session_id))
    return hits[0] if hits else None


def _usage_records(session_dir):
    recs = []
    for wf in glob.glob(os.path.join(session_dir, "agents", "*", "wire.jsonl")):
        for ln in open(wf, errors="ignore"):
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if o.get("type") == "usage.record" and isinstance(o.get("usage"), dict):
                recs.append(o)
    recs.sort(key=lambda r: r.get("time", ""))
    return recs


def _summ(recs):
    def call_in(u):
        return u.get("inputOther", 0) + u.get("inputCacheRead", 0) + u.get("inputCacheCreation", 0)
    return {
        "calls": len(recs),
        "peak_in": max((call_in(r["usage"]) for r in recs), default=0),
        "in_total": sum(call_in(r["usage"]) for r in recs),
        "in_cached": sum(r["usage"].get("inputCacheRead", 0) for r in recs),
        "out_total": sum(r["usage"].get("output", 0) for r in recs),
    }


def run_kimi(scn, model, workdir):
    prompts = scn["prompts"]
    session_id = None
    turns_out = []
    t0 = time.time()
    seen = 0   # usage records attributed to earlier turns
    for ti, p in enumerate(prompts):
        cmd = [KIMI_BIN] + (["-r", session_id] if session_id else []) + \
              ["-m", model, "-p", p, "--output-format", "stream-json"]
        tt = time.time()
        exit_status = "ok"
        try:
            proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                                  timeout=TURN_TIMEOUT)
            if proc.returncode != 0:
                exit_status = f"rc={proc.returncode}: {proc.stderr.strip()[-120:]}"
            for ln in proc.stdout.splitlines():
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if o.get("type") == "session.resume_hint" or o.get("role") == "meta":
                    session_id = o.get("session_id", session_id)
        except subprocess.TimeoutExpired:
            exit_status = f"timeout>{TURN_TIMEOUT}s"

        sdir = _session_dir(session_id) if session_id else None
        recs = _usage_records(sdir) if sdir else []
        turn_u = _summ(recs[seen:])
        seen = len(recs)
        turns_out.append({"turn": ti + 1, "exit": exit_status,
                          "wall_s": round(time.time() - tt, 1), **turn_u})
        print(f"  turn {ti+1:>2}/{len(prompts)}: {exit_status[:28]:<28} calls={turn_u['calls']:<3} "
              f"peak={turn_u['peak_in']/1000:>6.1f}k", flush=True)
        if session_id is None:
            print("  STOPPING: no session id — cannot resume", flush=True)
            break
        if exit_status.startswith("timeout"):
            print("  STOPPING: turn timeout (recorded as data)", flush=True)
            break

    ok, failed = scn["verify"](workdir)
    sdir = _session_dir(session_id) if session_id else None
    total = _summ(_usage_records(sdir)) if sdir else _summ([])
    return {
        "scenario": scn["name"], "agent": "kimi", "model": model,
        "passed": bool(ok), "failed_checks": list(failed or []),
        "turns_completed": sum(1 for t in turns_out if t["exit"] == "ok"),
        "turns_total": len(prompts), "session_id": session_id,
        "wall_s": round(time.time() - t0, 1), "turns": turns_out, **total,
    }


def _meterize(res, model):
    import sys as _s
    _s.path.insert(0, HERE)
    from meter import enrich, summarize
    res["turns"] = [{**t, **enrich(t, model)} if "in_total" in t else t for t in res.get("turns", [])]
    res.update(summarize([t for t in res.get("turns", []) if "in_total" in t], model))
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--model", default="deepseek/v4-flash")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    scn = load_scenario(a.scenario)
    workdir = tempfile.mkdtemp(prefix=f"kimi-arm-{a.scenario}-")
    scn["setup"](workdir)
    print(f"[{a.scenario}] {scn['meta']['turns']} turns · model={a.model} · workdir={workdir}")
    res = _meterize(run_kimi(scn, a.model, workdir), a.model)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, f"{a.scenario}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    print(f"pass={res['passed']} turns={res['turns_completed']}/{res['turns_total']} "
          f"peak={res['peak_in']/1000:.1f}k")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
