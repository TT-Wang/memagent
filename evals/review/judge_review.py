"""Judge review quality against ground-truth planted bugs. Symmetric: scores every agent's review
identically against the same truth.json, so any judge bias cancels in the sliceagent-vs-kimi comparison.

Metrics per agent:
  recall    = planted issues correctly identified / total planted     (did it catch the real bugs?)
  false_pos = claimed issues that are NOT real (hallucinated / padding / distractor-as-bug)
  precision = real findings / (real findings + false_pos)             (signal vs noise)
The dogfood failure (reporting a missing-but-actually-present dep as a blocker) shows up as false_pos.

Run: PYTHONPATH=src .venv/bin/python -m evals.review.judge_review --reviews evals/review/reviews_r1_taskq.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGETS = os.path.join(ROOT, "evals", "review", "targets")

RUBRIC = """You are scoring a code review against a GROUND-TRUTH list of planted bugs. Be strict and objective.

GROUND-TRUTH PLANTED ISSUES (each is a REAL bug):
{issues}

KNOWN DISTRACTORS (these are NOT bugs — code that looks suspicious but is correct):
{distractors}

THE REVIEW TO SCORE:
<<<REVIEW
{review}
REVIEW>>>

Do two things:
1. For EACH planted issue id, decide whether the review IDENTIFIED that specific bug — i.e. it names the
   same root problem (not merely the same file, not a vague gesture). Quote the matching sentence.
2. List every DISTINCT problem the review CLAIMS that is NOT one of the planted issues, and classify each:
   - "distractor_false_positive": it flags a known distractor as a bug
   - "false_positive": a claimed bug that is not real / unsubstantiated / padding / an environment artifact
     (e.g. claiming a dependency/test is broken without it actually being broken)
   - "plausible_extra": a genuinely real issue that just isn't in our planted set (be conservative — only
     if you are confident it is a true defect)

Output ONLY a JSON object, no prose:
{{"per_issue": [{{"id": "B1", "found": true/false, "evidence": "..."}}, ...],
  "extra_claims": [{{"claim": "...", "kind": "false_positive|distractor_false_positive|plausible_extra"}}, ...]}}"""


def _fmt_issues(truth):
    return "\n".join(f"- {i['id']} [{i['file']}] ({i['severity']} {i['type']}): {i['desc']}"
                     for i in truth["issues"])


def _fmt_distractors(truth):
    return "\n".join(f"- {d['id']} [{d['file']}] {d['ref']}: {d['desc']}" for d in truth.get("distractors", []))


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON in judge output")
    return json.loads(m.group(0))


def judge_one(truth: dict, review: str, llm) -> dict:
    if not review.strip():
        return {"recall": 0.0, "found": 0, "total": len(truth["issues"]), "false_pos": 0,
                "precision": 0.0, "per_issue": [], "extra_claims": [], "note": "empty review"}
    prompt = RUBRIC.format(issues=_fmt_issues(truth), distractors=_fmt_distractors(truth),
                           review=review[:12000])
    data = None
    last = None
    for _attempt in range(4):                 # transient Moonshot timeouts are common; retry
        try:
            resp = llm.complete([{"role": "user", "content": prompt}], [])
            data = _extract_json(resp.content or "")
            break
        except Exception as e:  # noqa: BLE001
            last = e
            continue
    if data is None:
        raise RuntimeError(f"judge failed after retries: {last}")
    per = data.get("per_issue", [])
    extra = data.get("extra_claims", [])
    found = sum(1 for p in per if p.get("found"))
    total = len(truth["issues"])
    false_pos = sum(1 for e in extra if e.get("kind") in ("false_positive", "distractor_false_positive"))
    plausible = sum(1 for e in extra if e.get("kind") == "plausible_extra")
    real_findings = found + plausible
    precision = real_findings / max(real_findings + false_pos, 1)
    return {"recall": round(found / max(total, 1), 3), "found": found, "total": total,
            "false_pos": false_pos, "plausible_extra": plausible, "precision": round(precision, 3),
            "per_issue": per, "extra_claims": extra}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviews", required=True)
    ap.add_argument("--model", default=os.environ.get("AGENT_JUDGE_MODEL", os.environ.get("AGENT_MODEL", "kimi-k2.7-code")))
    args = ap.parse_args()
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from sliceagent.cli import _load_env
    from sliceagent.llm import OpenAILLM
    _load_env()
    llm = OpenAILLM(model=args.model, timeout=120.0)

    blob = json.load(open(args.reviews))
    truth = json.load(open(os.path.join(TARGETS, blob["target"], "truth.json")))
    print(f"=== JUDGING {blob['target']} (judge model: {args.model}) ===\n")
    scores = {}
    for agent, r in blob["reviews"].items():
        s = judge_one(truth, r.get("review", ""), llm)
        scores[agent] = s
        missed = [p["id"] for p in s["per_issue"] if not p.get("found")]
        print(f"{agent:9} recall {s['found']}/{s['total']} ({s['recall']:.0%})  "
              f"false_pos {s['false_pos']}  precision {s['precision']:.0%}  "
              f"plausible_extra {s.get('plausible_extra',0)}")
        if missed:
            print(f"          missed: {missed}")
        for e in s["extra_claims"]:
            if e["kind"] != "plausible_extra":
                print(f"          FALSE+ ({e['kind']}): {e['claim'][:90]}")
    blob["scores"] = scores
    json.dump(blob, open(args.reviews, "w"), indent=2)
    print(f"\nupdated {args.reviews} with scores")
    # verdict
    if "sliceagent" in scores and "kimi" in scores:
        m, k = scores["sliceagent"], scores["kimi"]
        print("\n--- PARITY CHECK (sliceagent vs kimi) ---")
        print(f"  recall:    sliceagent {m['recall']:.0%} vs kimi {k['recall']:.0%}")
        print(f"  false_pos: sliceagent {m['false_pos']} vs kimi {k['false_pos']}")
        print(f"  precision: sliceagent {m['precision']:.0%} vs kimi {k['precision']:.0%}")


if __name__ == "__main__":
    main()
