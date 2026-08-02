"""One-command review-quality GATE (sliceagent only — no Kimi key needed). Runs sliceagent's review on every
planted-bug target with the CURRENT system prompt, LLM-judges each, and prints aggregate recall / false-pos
vs the parity baseline. Use it to gate a prompt change (e.g. the system-prompt dedupe): a PASS means the
edit didn't regress review thoroughness.

Run:
  cd ~/code/sliceagent
  set -a; source "/Users/tongtao/Desktop/agent design/.env"; set +a   # or however your keys are set
  export LLM_API_KEY="$OPENAI_API_KEY" AGENT_MODEL=gpt-5.5            # (+ LLM_BASE_URL / AGENT_JUDGE_MODEL if needed)
  PYTHONPATH=src .venv/bin/python -m evals.review.gate
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

TARGETS = ["r1_taskq", "r2_miniweb", "r3_statkit"]
# The review-quality-parity baseline sliceagent reached vs Kimi-Code (recall ~0.92, ~0.63 false-pos/review).
BASELINE_RECALL = 0.92
BASELINE_FP = 0.63
RECALL_TOL = 0.07     # n=3 is small; allow noise, fail only on a real drop
FP_TOL = 0.5


def main() -> int:
    from sliceagent.cli import _load_env
    _load_env()
    from sliceagent.llm import OpenAILLM
    from evals.review.judge_review import judge_one
    from evals.review.run_review import _copy_code, _load_target, run_sliceagent

    model = os.environ.get("AGENT_MODEL", "gpt-5.5")
    judge_llm = OpenAILLM(model=os.environ.get("AGENT_JUDGE_MODEL", model), timeout=90.0)

    rows = []
    for t in TARGETS:
        truth = _load_target(t)
        wd = _copy_code(t)
        try:
            r = run_sliceagent(truth["review_task"], wd, model)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        if r.get("error"):
            print(f"{t}: ERROR {r['error']}")
        j = judge_one(truth, r.get("review", ""), judge_llm)
        rows.append(j)
        print(f"{t:12} recall={j['recall']:.2f}  found={j['found']}/{j['total']}  "
              f"false_pos={j['false_pos']}  precision={j['precision']:.2f}")

    n = max(len(rows), 1)
    recall = sum(j["recall"] for j in rows) / n
    fp = sum(j["false_pos"] for j in rows) / n
    print(f"\nAGGREGATE  recall={recall:.3f}  false_pos/review={fp:.2f}   "
          f"(parity baseline ~{BASELINE_RECALL}/{BASELINE_FP})")
    ok = recall >= BASELINE_RECALL - RECALL_TOL and fp <= BASELINE_FP + FP_TOL
    print("GATE:", "PASS — prompt dedupe did NOT regress review quality"
          if ok else "FAIL — review quality regressed; `git revert 773033a`")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
