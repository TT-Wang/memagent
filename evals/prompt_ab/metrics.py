"""Per-trial metric runners for the prompt A/B suite. ONE trial = one full pass over a metric's items.

Run as a subprocess with the variant prompt injected + the provider env already set:
  SLICEAGENT_PROMPT_FILE=<variant.txt> LLM_API_KEY=... LLM_BASE_URL=... AGENT_MODEL=... \
  PYTHONPATH=src:evals .venv/bin/python -m evals.prompt_ab.metrics --metric review --model M --out F

Writes {"metric","model","items":[{item, <scores>}, ...]} to --out. Reuses the existing benches so the
scoring is identical to the standalone gates:
  review  -> evals.review (recall, false_pos, precision) over 3 planted-bug targets
  convo   -> evals.convo_h2h (intent_ok, smoothness 1-10 LLM-judge, length_chars) over 6 cases
  tasks   -> evals.hardbench (passed 0/1) over 3 staged multi-turn scenarios
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REVIEW_TARGETS = ["r1_taskq", "r2_miniweb", "r3_statkit"]


def _judge_llm(judge_model):
    from sliceagent.llm import OpenAILLM
    return OpenAILLM(model=judge_model, timeout=90.0)


def review_trial(model, judge_model):
    from evals.review.judge_review import judge_one
    from evals.review.run_review import _copy_code, _load_target, run_sliceagent
    judge = _judge_llm(judge_model)
    items = []
    for t in REVIEW_TARGETS:
        truth = _load_target(t)
        wd = _copy_code(t)
        try:
            r = run_sliceagent(truth["review_task"], wd, model)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        j = judge_one(truth, r.get("review", ""), judge)
        items.append({"item": t, "recall": float(j["recall"]), "false_pos": float(j["false_pos"]),
                      "precision": float(j["precision"])})
    return items


def _smoothness(judge, user_msg, answer):
    if not (answer or "").strip():
        return 1.0
    prompt = ("Rate from 1 to 10 how NATURAL, helpful, and appropriately concise this assistant reply is to "
              "the user message. 10 = a great human-like reply; 1 = robotic, off-target, or over/under-done. "
              "Answer with ONLY the integer.\n\nUSER: " + user_msg + "\n\nREPLY: " + answer)
    try:
        resp = judge.complete([{"role": "user", "content": prompt}], [])
        digits = "".join(c for c in (resp.content or "") if c.isdigit())
        return float(min(max(int(digits[:2]), 1), 10)) if digits else 5.0
    except Exception:
        return 5.0


def convo_trial(model, judge_model):
    from evals.convo_h2h import PROMPTS, _intent_flags, _make_workdir, run_sliceagent
    judge = _judge_llm(judge_model)
    items = []
    for (cid, prompt, kind, expect) in PROMPTS:
        wd = _make_workdir()
        try:
            r = run_sliceagent(prompt, wd, model)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        fl = _intent_flags(kind, expect, r["tools"])
        intent_ok = 0.0 if (fl["fail_edited_a_question"] or fl["fail_overtooled_chat"]) else 1.0
        items.append({"item": cid, "intent_ok": intent_ok,
                      "smoothness": _smoothness(judge, prompt, r.get("text", "")),
                      "length_chars": float(len(r.get("text", "")))})
    return items


def tasks_trial(model, judge_model):
    os.environ["AGENT_MODEL"] = model            # hardbench.run reads MODEL from env at import time
    sys.path.insert(0, os.path.join(ROOT, "evals"))
    from hardbench.run import run_sliceagent as hb_run
    from hardbench.scenarios import SCENARIOS
    items = []
    for name, sc in SCENARIOS.items():
        try:
            r = hb_run(name, sc)
            items.append({"item": name, "passed": 1.0 if r.get("passed") else 0.0})
        except Exception as e:  # noqa: BLE001
            items.append({"item": name, "passed": 0.0, "error": f"{type(e).__name__}: {e}"[:160]})
    return items


RUNNERS = {"review": review_trial, "convo": convo_trial, "tasks": tasks_trial}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metric", required=True, choices=list(RUNNERS))
    ap.add_argument("--model", required=True)
    ap.add_argument("--judge-model", default="")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    sys.path.insert(0, os.path.join(ROOT, "src"))
    items = RUNNERS[a.metric](a.model, a.judge_model or a.model)
    json.dump({"metric": a.metric, "model": a.model, "items": items}, open(a.out, "w"), indent=2)
    print(f"[{a.metric}] {len(items)} items -> {a.out}")


if __name__ == "__main__":
    main()
