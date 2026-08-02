"""Slipstream-style decision-equivalence replay: the cheap counterfactual layer for the 28-region
schema A/B (convergence spec P1.2 / P2.1).

For each recorded turn, re-decide TWICE from the sealed artifact's verbatim slice string —
once from the FULL recorded slice (baseline arm) and once with ONE region textually ablated —
and score whether the model makes the SAME first decision. The metric is DIFFERENTIAL
(ablated vs full-replay), so model/system-prompt drift since recording cancels out; the
full-replay vs RECORDED-action agreement is reported as a calibration column, never the metric.

Design commitments (from the verification sweep — do not regress):
- Ablation is TEXTUAL on the recorded ``steps[0].slice`` string. NEVER recompile: the compile
  path re-reads the live disk/git/retrieval and would contaminate the counterfactual.
- First-decision granularity only: SliceBuilt fires once per turn, so only decision #1 has an
  exactly recorded (slice -> action) pair.
- Two judge tiers reported SEPARATELY: exact (tool name + canonical_tool_args, the 'note' key
  already excluded) and, with --semantic, an LLM tier consulted ONLY on exact mismatches.
- A shared NEUTRAL system prompt in both arms (the original is not in the sealed record — a
  known validity limit; the forward-recording sink closes it for future corpora).
- Resumable per h2h convention: results persist after EACH cell, done cells skip.
- One usage row per model call (per-call peak is max-over-calls, never a per-stage sum).

Usage:
  .venv/bin/python -m evals.slipstream_replay --regions conversation,corrections --sample 20
  .venv/bin/python -m evals.slipstream_replay --list-regions        # corpus census, no spend
  .venv/bin/python -m evals.slipstream_replay --regions all --sample 30 --semantic
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import tempfile
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))
sys.path.insert(0, os.path.join(_REPO, "packages", "sliceagent-core", "src"))
sys.path.insert(0, os.path.join(_REPO, "packages", "sliceagent-cli", "src"))

from sliceagent_core.tool_identity import canonical_tool_args  # noqa: E402

OUT_DIR = os.path.join(_REPO, "evals", "slipstream")
RESULTS = os.path.join(OUT_DIR, "results.json")

# Region name -> rendered header prefix (the exact machine literals the renderers own). Sections
# whose header matches no entry are NEVER ablated (safety: unknown text stays). Adjacency blocks
# are one logical lane, ablated together under the pseudo-region name 'adjacency'.
REGION_HEADERS = {
    "intent": "# ACTIVE USER INTENT",
    "task_objective": "# STABLE TASK OBJECTIVE",
    "corrections": "# RETAINED USER CORRECTIONS",
    "task_constraints": "# PARENT TASK CONSTRAINTS",
    "open_files": "# OPEN FILES",
    "related_code": "# RELATED CODE",
    "skills": "# ACTIVE SKILL(S)",
    "memory": "# RELEVANT KNOWLEDGE CANDIDATES",
    "conversation": "# RECENT CONVERSATION",
    "findings": "# YOUR NOTES FROM PRIOR TOOL CALLS",
    "progress": "# PROGRESS SIGNALS",
    "world": "# WORLD MODEL",
    "threads": "# OTHER OPEN THREADS",
    "cache_manifest": "# PAGED-OUT HISTORY",
    "action_header": "# REPEATED/FAILING ACTIONS",
    "evidence_result": "# AUTHORITATIVE EVIDENCE RESULT",
    "evidence_detail": "# MATCHED EVIDENCE DETAIL",
    "quality_evidence_result": "# QUALITY EVIDENCE PROTOCOL",
    "quality_evidence_detail": "# EXACT SEALED REQUEST/RESPONSE PAIRS",
    "turn_contract": "# TURN CONTRACT",
    "focus": "# CURRENT PROJECT",
    "worktree": "# REPO STATE",
    "user_report": "# OPEN USER REPORT",
    "error": "# CURRENT ERROR",
    "adjacency": "# IMMEDIATE PRIOR EXCHANGE",   # + EARLIER EXCHANGE, see _section_region
    "world_state": "# WORLD STATE",              # legacy-era records
    "active_work": "# ACTIVE WORK",
}
# NEVER ablate (user-authority semantics are not experimented on — spec 2.1 先验声明):
MANDATORY_SKIP = {"intent", "corrections", "user_report", "task_objective", "turn_contract"}

NEUTRAL_SYSTEM = (
    "You are a coding agent operating on a workspace. Decide your NEXT single step for the "
    "user's current request using ONLY the provided context. If a tool is needed, call exactly "
    "one tool; if the request is fully answerable from context, reply with the final answer and "
    "no tool call."
)


def _section_region(header_line: str) -> str | None:
    if header_line.startswith("# EARLIER EXCHANGE"):
        return "adjacency"
    best = None
    for region, prefix in REGION_HEADERS.items():
        if header_line.startswith(prefix) and (best is None or len(prefix) > len(REGION_HEADERS[best])):
            best = region
    return best


def split_sections(slice_text: str) -> list[tuple[str | None, str]]:
    """[(region|None, section_text)] — '# ' lines open sections; leading text has region None."""
    out: list[tuple[str | None, str]] = []
    cur_region, cur_lines = None, []
    for line in slice_text.splitlines(keepends=True):
        if line.startswith("# "):
            if cur_lines:
                out.append((cur_region, "".join(cur_lines)))
            cur_region, cur_lines = _section_region(line), [line]
        else:
            cur_lines.append(line)
    if cur_lines:
        out.append((cur_region, "".join(cur_lines)))
    return out


def ablate(slice_text: str, region: str) -> str | None:
    """The slice minus one region's sections; None when the region is absent (skip the cell —
    an absent-region 'equivalence' would fake signal)."""
    sections = split_sections(slice_text)
    if not any(r == region for r, _ in sections):
        return None
    return "".join(text for r, text in sections if r != region)


_REDACTED_RUN = re.compile(r"(.)\1{199,}")


def load_corpus(sample_per_region: int) -> list[dict]:
    rows = []
    for path in sorted(glob.glob(os.path.expanduser("~/.sliceagent/core/*/artifacts/*/turn-*.json"))):
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception:  # noqa: BLE001 — corrupt record: skip, never crash the sweep
            continue
        if d.get("kind") != "turn":
            continue
        sb = d.get("structured_body") or {}
        steps = sb.get("steps") or []
        if not steps:
            continue
        slice_text = str(steps[0].get("slice") or "")
        if not slice_text.strip() or _REDACTED_RUN.search(slice_text):
            continue   # redacted-secret turns replay differently by construction — excluded
        actions = steps[0].get("action") or []
        rows.append({
            "artifact_id": str(d.get("id") or os.path.basename(path)),
            "slice": slice_text,
            "recorded_action": (actions[0] if actions else None),   # None => final-answer turn
        })
    return rows


def _tool_roster():
    try:
        from sliceagent_cli.tools import LocalToolHost
        return LocalToolHost(tempfile.mkdtemp(prefix="slipstream-roster-")).schemas()
    except Exception:  # noqa: BLE001 — fall back to a minimal but stable roster
        def t(name, props, req):
            return {"type": "function", "function": {"name": name, "parameters": {
                "type": "object", "properties": props, "required": req}}}
        p = {"path": {"type": "string"}}
        return [t("read_file", p, ["path"]), t("list_files", p, []),
                t("grep", {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
                t("run_command", {"command": {"type": "string"}}, ["command"]),
                t("edit_file", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])]


def decide(llm, roster, slice_text: str) -> tuple[dict | None, dict]:
    """One re-decision. Returns (action|None, usage_row). action={'name','args'}."""
    t0 = time.time()
    resp = llm.complete(
        [{"role": "system", "content": NEUTRAL_SYSTEM},
         {"role": "user", "content": slice_text}], roster,
    )
    usage = (getattr(resp, "usage", None) or {})
    row = {"prompt": usage.get("prompt_tokens", 0), "completion": usage.get("completion_tokens", 0),
           "wall_s": round(time.time() - t0, 2)}
    calls = getattr(resp, "tool_calls", None) or []
    if not calls:
        return None, row
    call = calls[0]
    return {"name": str(call.name), "args": dict(call.args or {})}, row


def exact_equivalent(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return a["name"] == b["name"] and canonical_tool_args(a["args"]) == canonical_tool_args(b["args"])


_JUDGE_PROMPT = (
    "Two coding-agent decisions for the same context are below. Answer EQUIVALENT only if they "
    "pursue the same immediate goal on the same target (e.g. read_file vs grep on the same file "
    "for the same info counts; different targets or different goals do not). Answer with exactly "
    "one word: EQUIVALENT or DIFFERENT.\nA: {a}\nB: {b}"
)


def semantic_equivalent(llm, a, b) -> bool:
    try:
        resp = llm.complete([{"role": "user", "content": _JUDGE_PROMPT.format(
            a=json.dumps(a, ensure_ascii=False), b=json.dumps(b, ensure_ascii=False))}], [])
        return "EQUIVALENT" in str(getattr(resp, "content", "") or "").upper()
    except Exception:  # noqa: BLE001 — judge failure = not equivalent (conservative)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--regions", default="conversation,findings,progress,world,cache_manifest",
                    help="comma list, or 'all' (mandatory user-authority regions always skipped)")
    ap.add_argument("--sample", type=int, default=20, help="turns per region (deterministic order)")
    ap.add_argument("--semantic", action="store_true", help="LLM tier on exact mismatches")
    ap.add_argument("--model", default=os.environ.get("AGENT_MODEL", "deepseek-v4-flash"))
    ap.add_argument("--list-regions", action="store_true", help="corpus census only, no model calls")
    ap.add_argument("--aa-control", action="store_true",
                    help="A/A self-consistency ceiling: re-decide the FULL slice twice per sampled "
                         "turn; region equivalence rates are read AGAINST this ceiling, never as "
                         "absolutes (pin LLM_TEMPERATURE=0 for the sharpest reading)")
    args = ap.parse_args()

    corpus = load_corpus(args.sample)
    print(f"corpus: {len(corpus)} usable recorded turns")
    if args.list_regions:
        counts: dict[str, int] = {}
        for row in corpus:
            for r, _ in split_sections(row["slice"]):
                if r:
                    counts[r] = counts.get(r, 0) + 1
        for r, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            flag = " (mandatory — never ablated)" if r in MANDATORY_SKIP else ""
            print(f"  {r:26s} {n:4d} turns{flag}")
        return 0

    regions = ([r for r in REGION_HEADERS if r not in MANDATORY_SKIP]
               if args.regions == "all" else
               [r.strip() for r in args.regions.split(",") if r.strip()])
    bad = [r for r in regions if r in MANDATORY_SKIP]
    if bad:
        print(f"refusing to ablate mandatory user-authority regions: {bad} (spec 2.1)")
        return 2

    os.makedirs(OUT_DIR, exist_ok=True)
    results: dict = {}
    if os.path.exists(RESULTS):
        results = json.load(open(RESULTS, encoding="utf-8"))

    def persist():
        json.dump(results, open(RESULTS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    from sliceagent_core.llm import OpenAILLM
    llm = OpenAILLM(model=args.model)
    roster = _tool_roster()

    if args.aa_control:
        done = agree = 0
        for row in corpus:
            if done >= args.sample:
                break
            key = f"{row['artifact_id']}:__aa__"
            if key in results:
                done += 1
                agree += 1 if results[key]["exact_equivalent"] else 0
                continue
            a1, u1 = decide(llm, roster, row["slice"])
            a2, u2 = decide(llm, roster, row["slice"])
            eq = exact_equivalent(a1, a2)
            results[key] = {"exact_equivalent": eq, "a": a1, "b": a2,
                            "usage": [u1, u2], "temperature": os.environ.get("LLM_TEMPERATURE", "")}
            persist()
            done += 1
            agree += 1 if eq else 0
        print(f"A/A self-consistency ceiling: {agree}/{done} "
              f"({agree / done:.0%}) at LLM_TEMPERATURE={os.environ.get('LLM_TEMPERATURE', '(default)')}"
              if done else "A/A: no cells")
        return 0
    full_cache: dict[str, dict] = {}   # artifact_id -> {"action":..., "usage":...} baseline this run/prior

    for region in regions:
        done = spent = 0
        for row in corpus:
            if done >= args.sample:
                break
            ablated = ablate(row["slice"], region)
            if ablated is None:
                continue   # region absent from this turn — no counterfactual to measure
            key = f"{row['artifact_id']}:{region}"
            if key in results:
                done += 1
                continue
            base_key = f"{row['artifact_id']}:__full__"
            if base_key in results:
                base = results[base_key]
            elif row["artifact_id"] in full_cache:
                base = full_cache[row["artifact_id"]]
            else:
                action, usage = decide(llm, roster, row["slice"])
                base = {"action": action, "usage": usage,
                        "matches_recorded": exact_equivalent(action, (
                            {"name": row["recorded_action"]["name"],
                             "args": dict(row["recorded_action"].get("args") or {})}
                            if row["recorded_action"] else None))}
                results[base_key] = base
                full_cache[row["artifact_id"]] = base
                persist()
            abl_action, abl_usage = decide(llm, roster, ablated)
            exact = exact_equivalent(abl_action, base["action"])
            cell = {"region": region, "exact_equivalent": exact,
                    "ablated_action": abl_action, "full_action": base["action"],
                    "usage": abl_usage}
            if not exact and args.semantic:
                cell["semantic_equivalent"] = semantic_equivalent(llm, abl_action, base["action"])
            results[key] = cell
            persist()
            done += 1
            spent += 1
        print(f"region {region}: {done} cells ({spent} new)")

    # summary — the A/B input table
    print("\n== decision-equivalence by region (ablated vs full-replay) ==")
    for region in regions:
        cells = [v for k, v in results.items() if v.get("region") == region]
        if not cells:
            continue
        n = len(cells)
        exact = sum(1 for c in cells if c["exact_equivalent"])
        sem = sum(1 for c in cells if c.get("semantic_equivalent") or c["exact_equivalent"])
        print(f"  {region:26s} exact {exact}/{n} ({exact / n:.0%})"
              + (f" · +semantic {sem}/{n} ({sem / n:.0%})" if args.semantic else ""))
    bases = [v for k, v in results.items() if k.endswith(":__full__")]
    if bases:
        cal = sum(1 for b in bases if b.get("matches_recorded"))
        print(f"  calibration: full-replay matches RECORDED action {cal}/{len(bases)} "
              f"({cal / len(bases):.0%}) — drift indicator, not the metric")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
