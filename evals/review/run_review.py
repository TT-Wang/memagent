"""Review-quality h2h driver: run sliceagent AND kimi on the SAME 'review this code' task over a
controlled target with ground-truth planted bugs, capture each agent's review TEXT, and hand both to
the judge (judge_review.py). Apple-to-apple: same model (kimi-k2.7-code), same workdir copy, same task.

Run:
  export LLM_API_KEY=$MOONSHOT_API_KEY LLM_BASE_URL=https://api.moonshot.cn/v1 AGENT_MODEL=kimi-k2.7-code
  PYTHONPATH=src .venv/bin/python -m evals.review.run_review --target r1_taskq --agents sliceagent,kimi
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGETS = os.path.join(ROOT, "evals", "review", "targets")
KIMI_BIN = os.path.expanduser("~/.kimi-code/bin/kimi")
KIMI_TIMEOUT = 300


def _load_target(name: str) -> dict:
    return json.load(open(os.path.join(TARGETS, name, "truth.json")))


def _copy_code(name: str) -> str:
    """Copy ONLY the code (not truth.json) into a fresh workdir the agent reviews."""
    src = os.path.join(TARGETS, name)
    wd = tempfile.mkdtemp(prefix=f"review-{name}-")
    for entry in os.listdir(src):
        if entry == "truth.json":
            continue
        s = os.path.join(src, entry)
        d = os.path.join(wd, entry)
        shutil.copytree(s, d) if os.path.isdir(s) else shutil.copy2(s, d)
    return wd


def _pull_text(obj, out: list):
    """Recursively collect assistant text from a kimi stream-json event. Kimi uses 'content' (string)
    for assistant messages and 'text' for content blocks; tool-call 'arguments' are NOT review text."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "arguments":            # tool-call args (file paths etc.) — not the review
                continue
            if k in ("content", "text") and isinstance(v, str):
                out.append(v)
            else:
                _pull_text(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _pull_text(x, out)


def run_kimi(task: str, wd: str) -> dict:
    from evals.h2h_run import _kimi_session_dir, _kimi_usage
    cmd = [KIMI_BIN, "-p", task, "--output-format", "stream-json"]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=wd, capture_output=True, text=True, timeout=KIMI_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"agent": "kimi", "review": "", "wall_s": KIMI_TIMEOUT, "error": "timeout"}
    chunks: list[str] = []
    session_id = None
    for ln in proc.stdout.splitlines():
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("type") == "session.resume_hint" or o.get("role") == "meta":
            session_id = o.get("session_id", session_id)
        _pull_text(o, chunks)
    review = "\n".join(c for c in chunks if c.strip())
    if not review:                      # fallback: some builds print plain text
        review = proc.stdout.strip()
    sd = _kimi_session_dir(session_id) if session_id else None
    u = _kimi_usage(sd) if sd else {"in_total": 0, "in_cached": 0, "out_total": 0, "peak_in": 0, "calls": 0}
    return {"agent": "kimi", "review": review, "wall_s": round(time.time() - t0, 1),
            "rc": proc.returncode, "error": "" if review else proc.stderr.strip()[-200:],
            "in_total": u["in_total"], "in_cached": u["in_cached"], "out_total": u["out_total"],
            "peak_in": u["peak_in"], "steps": u.get("calls", 0)}


def run_sliceagent(task: str, wd: str, model: str) -> dict:
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from sliceagent.pfc import Slice, slice_sink, record_user
    from sliceagent.seed import make_build_slice
    from sliceagent.loop import run_turn
    from sliceagent.tools import LocalToolHost
    from sliceagent.code_index import make_code_index
    from sliceagent.memory import NullMemory
    from sliceagent.events import AssistantText, make_dispatcher
    from sliceagent.hooks import CatastrophicSafeguardHook, CompositeHooks
    from sliceagent.llm import OpenAILLM

    state = Slice(); state.reset(task)
    tools = LocalToolHost(root=wd)
    retriever = make_code_index(wd)
    chunks: list[str] = []

    def _cap(e):
        if isinstance(e, AssistantText) and e.content and e.content.strip():
            chunks.append(e.content.strip())

    from evals.h2h_run import _UsageTap
    dispatch = make_dispatcher(slice_sink(state), _cap)
    hooks = CompositeHooks(CatastrophicSafeguardHook())
    tap = _UsageTap(OpenAILLM(model=model, timeout=90.0))
    record_user(state, task)
    build = make_build_slice(state, tools, retriever, NullMemory(), task)
    t0 = time.time()
    try:
        res = run_turn(build_slice=build, llm=tap, tools=tools, dispatch=dispatch, hooks=hooks, max_steps=24)
        err, steps = "", res.steps
    except Exception as e:  # noqa: BLE001
        err, steps = f"{type(e).__name__}: {e}", -1
    c = tap.calls
    return {"agent": "sliceagent", "review": "\n\n".join(chunks), "wall_s": round(time.time() - t0, 1),
            "steps": steps, "error": err,
            "in_total": sum(x["prompt"] for x in c), "in_cached": sum(x["cached"] for x in c),
            "out_total": sum(x["completion"] for x in c), "peak_in": max((x["prompt"] for x in c), default=0)}


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--agents", default="sliceagent,kimi")
    ap.add_argument("--model", default=os.environ.get("AGENT_MODEL", "kimi-k2.7-code"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from sliceagent.cli import _load_env
    _load_env()

    truth = _load_target(args.target)
    task = truth["review_task"]
    out = {"target": args.target, "model": args.model, "reviews": {}}
    for agent in args.agents.split(","):
        wd = _copy_code(args.target)
        print(f"\n=== {agent} reviewing {args.target} ===")
        try:
            r = run_kimi(task, wd) if agent == "kimi" else run_sliceagent(task, wd, args.model)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        out["reviews"][agent] = r
        it, ic, ot = r.get("in_total", 0), r.get("in_cached", 0), r.get("out_total", 0)
        # kimi-k2.7-code rates: $0.95/M miss, $0.19/M hit, $4.00/M out
        cost = (it - ic) / 1e6 * 0.95 + ic / 1e6 * 0.19 + ot / 1e6 * 4.00
        r["cost_usd"] = round(cost, 5)
        print(f"  wall={r.get('wall_s')}s steps={r.get('steps')} err={r.get('error') or 'none'} "
              f"in={it} (cached {ic}) out={ot} peak_in={r.get('peak_in',0)} cost=${cost:.5f}")
    path = args.out or os.path.join(ROOT, "evals", "review", f"reviews_{args.target}.json")
    json.dump(out, open(path, "w"), indent=2)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
