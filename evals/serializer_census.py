"""Exact serializer census — FREE byte/token attribution at the last local provider boundary.

Runs ONE benchmark scenario and, per model call, captures the EXACT messages list + tool schemas at
the point they are handed to the inner LLM adapter (the last local boundary before the provider SDK),
then decomposes the full serialization into attributed parts:

  - system message
  - tool schemas (serialized once per call)
  - seed user message (msg1), split into: pre-header envelope bytes, per-region chunks split on
    serialized "\\n# " headers (region name = first 36 chars of the header line), and the
    CURRENT REQUEST + NOW frame (bytes after the last "</context>" marker)
  - each assistant message, content vs tool_calls JSON separately (the "assistant call envelope"
    an external audit could not measure from billing data — here we capture it locally); the
    message's JSON key/brace overhead and any reasoning_content are counted in the envelope bucket
  - each tool-result message, and any other user messages (steering/advisory injections)

RECONCILIATION GATE (the audit's exit criterion): the parts must sum EXACTLY, in chars, to
len(json.dumps(messages, ensure_ascii=False)) + len(json.dumps(schemas, ensure_ascii=False)).
A nonzero gap is printed loudly and marks the run INVALID — never silently truncated. The gate is
real, not by-construction: message-list overhead is computed analytically (brackets + ", "
separators) and nested-value serializations are computed independently, so any change in
json.dumps behavior or a bad span split breaks the sum.

Token estimates are CALIBRATED per run from the provider's own ledger (meter.in_total / summed
prompt bytes) — the repo's static _TOKENS_PER_BYTE is a runtime budgeting constant measured 1.63x
off the billed ratio, which corrupted every dollar attribution while leaving shares intact. A
>10% mismatch between the category sum and meter.in_total marks the run INVALID. Raw char and UTF-8
byte counts are always reported alongside.

Also measured per call: whether the seed user message (msg1) is byte-identical to the previous
call's msg1 within the same turn — the per-call re-projection churn, reported in the aggregates.

Costs $0: the selftest drives the bench with a scripted fake LLM; a real run needs a configured
provider exactly like benchmarks/run.py. Output JSON (which may contain request content) goes to
evals/census_runs/ — gitignored, stays local. Stdout prints counts and region names only.

Usage:
  .venv/bin/python evals/serializer_census.py --scenario s2_taskdag_scheduler --label base
  .venv/bin/python evals/serializer_census.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "benchmarks"))
sys.path.insert(0, os.path.join(ROOT, "src"))

_spec = importlib.util.spec_from_file_location("bench_run", os.path.join(ROOT, "benchmarks", "run.py"))
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

try:  # the repo's own estimator ratio — never claim provider-exact tokens
    from sliceagent_core.execution import _TOKENS_PER_BYTE as _TPB
    _ESTIMATOR = "sliceagent_core.execution._TOKENS_PER_BYTE"
except Exception:  # noqa: BLE001 - estimator is reporting-only; fall back to the documented ratio
    try:
        from sliceagent.execution import _TOKENS_PER_BYTE as _TPB  # type: ignore[no-redef]
        _ESTIMATOR = "sliceagent.execution._TOKENS_PER_BYTE"
    except Exception:  # noqa: BLE001
        _TPB = 1.15 / 3
        _ESTIMATOR = "fallback(1.15/3)"

_ESC_HDR = "\\n# "           # a "\n# " region header as it appears inside the JSON serialization
_CTX_CLOSE = "</context>"    # seed envelope closer; CURRENT REQUEST + NOW render after the last one


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _est_tokens(byte_len: int) -> int:
    return int(byte_len * _TPB)


def _part(cat: str, seg: str) -> dict:
    return {"cat": cat, "chars": len(seg), "bytes": len(seg.encode("utf-8", "replace"))}


CAP: dict = {"calls": []}


class _CensusTap(bench._Tap):
    """bench._Tap subclass capturing the exact per-call request at the adapter boundary.

    Serialization happens AT CAPTURE TIME: the loop mutates message dicts in place between calls
    (old tool-result bodies are cleared), so holding references would corrupt earlier captures.
    """

    def _capture(self, messages, schemas):
        msgs = []
        for m in messages:
            row: dict = {"role": m.get("role", "") if isinstance(m, dict) else "", "ser": _dump(m)}
            if isinstance(m, dict) and row["role"] == "assistant":
                vals = {}
                for key in ("content", "tool_calls", "reasoning_content"):
                    if key in m:
                        vs = _dump(m[key])
                        vals[key] = [len(vs), len(vs.encode("utf-8", "replace"))]
                row["vals"] = vals
            msgs.append(row)
        seed_sha = ""
        if len(msgs) > 1 and msgs[1]["role"] == "user":
            seed_sha = hashlib.sha1(msgs[1]["ser"].encode("utf-8", "replace")).hexdigest()
        CAP["calls"].append({
            "full_ser_len": len(_dump(messages)),   # independent whole for the reconciliation gate
            "msgs": msgs,
            "schemas_ser": _dump(schemas or []),
            "n_schemas": len(schemas or []),
            "seed_sha": seed_sha,
        })

    def complete(self, messages, tools):
        self._capture(messages, tools)
        return super().complete(messages, tools)

    def complete_with_control(self, messages, schemas, **kw):
        self._capture(messages, schemas)
        return super().complete_with_control(messages, schemas, **kw)


def _region_name(ser: str, hdr: int) -> str:
    """Region name from a serialized '\\n# ' header — same recognition as evals/spine_probe.py."""
    start = hdr + len(_ESC_HDR)
    end = len(ser)
    for stop in ("\\n", " (", "\\u"):
        cut = ser.find(stop, start)
        if 0 < cut < end:
            end = cut
    return ser[start:min(end, start + 36)].strip() or "_unnamed"


def _seed_parts(ser: str) -> list[dict]:
    """Contiguous, non-overlapping spans covering the seed message's ENTIRE serialization."""
    ctx = ser.rfind(_CTX_CLOSE)
    scan_end = (ctx + len(_CTX_CLOSE)) if ctx >= 0 else len(ser)
    hdrs = []
    pos = 0
    while True:
        hit = ser.find(_ESC_HDR, pos)
        if hit < 0 or hit >= scan_end:
            break
        hdrs.append(hit)
        pos = hit + len(_ESC_HDR)
    parts = [_part("seed:_pre_headers", ser[0:hdrs[0] if hdrs else scan_end])]
    for j, h in enumerate(hdrs):
        end = hdrs[j + 1] if j + 1 < len(hdrs) else scan_end
        parts.append(_part(f"seed:{_region_name(ser, h)}", ser[h:end]))
    if ctx >= 0:
        parts.append(_part("seed:_current_request_now", ser[scan_end:]))
    return parts


def _decompose_call(call: dict) -> tuple[list[dict], int, int, str]:
    """Return (parts, whole_chars, gap, error). gap != 0 or error => the call is INVALID."""
    msgs = call["msgs"]
    n = len(msgs)
    # json.dumps(list) = "[" + ", ".join(items) + "]" with default separators; each item serializes
    # exactly as json.dumps(item) alone. Computed analytically so the gate can catch it breaking.
    overhead = 2 + 2 * max(0, n - 1)
    parts = [{"cat": "msg_list_overhead", "chars": overhead, "bytes": overhead},
             _part("schemas", call["schemas_ser"])]
    error = ""
    for i, m in enumerate(msgs):
        role, ser = m["role"], m["ser"]
        if i == 0 and role == "system":
            parts.append(_part("system", ser))
        elif i == 1 and role == "user":
            parts.extend(_seed_parts(ser))
        elif role == "assistant":
            vals = m.get("vals", {})
            claimed_c = claimed_b = 0
            for key, cat in (("content", "assistant_content"), ("tool_calls", "assistant_tool_calls"),
                             ("reasoning_content", "assistant_reasoning")):
                if key in vals:
                    c, b = vals[key]
                    parts.append({"cat": cat, "chars": c, "bytes": b})
                    claimed_c += c
                    claimed_b += b
            env_c = len(ser) - claimed_c
            env_b = len(ser.encode("utf-8", "replace")) - claimed_b
            if env_c < 0 or env_b < 0:
                error = (f"assistant envelope residual negative (chars={env_c}, bytes={env_b}) — "
                         "nested-value serialization assumption broken")
            parts.append({"cat": "assistant_json_envelope", "chars": env_c, "bytes": env_b})
        elif role == "tool":
            parts.append(_part("tool_result", ser))
        elif role == "user":
            parts.append(_part("other_user", ser))
        else:
            parts.append(_part("other_message", ser))
    whole = call["full_ser_len"] + len(call["schemas_ser"])
    gap = whole - sum(p["chars"] for p in parts)
    return parts, whole, gap, error


_ROLLUP = {  # audit-facing coarse categories
    "system": "system", "schemas": "schemas", "msg_list_overhead": "structural",
    "other_message": "structural", "assistant_content": "assistant_prose",
    "assistant_tool_calls": "assistant_tool_call_envelope",
    "assistant_reasoning": "assistant_tool_call_envelope",
    "assistant_json_envelope": "assistant_tool_call_envelope",
    "tool_result": "tool_results", "other_user": "other_user",
}


def _rollup_cat(cat: str) -> str:
    return "seed" if cat.startswith("seed:") else _ROLLUP.get(cat, cat)


def summarize(res: dict, calls: list[dict], *, scenario: str, label: str, wall_s: float) -> dict:
    turn_of: list[int] = []
    for t in res.get("per_turn", ()):
        turn_of += [t["turn"]] * t["calls"]

    rows = []
    totals: dict[str, dict] = {}
    rollup: dict[str, dict] = {}
    split = {"first_calls": {}, "later_calls": {}, "unmapped_calls": {}}
    invalid = ""
    same_turn_pairs = 0
    mutations = 0
    for i, call in enumerate(calls):
        parts, whole, gap, error = _decompose_call(call)
        turn = turn_of[i] if i < len(turn_of) else None
        first = turn is None or i == 0 or i - 1 >= len(turn_of) or turn_of[i - 1] != turn
        bucket = "unmapped_calls" if turn is None else ("first_calls" if first else "later_calls")
        msg1_same = None
        if turn is not None and not first and call["seed_sha"] and calls[i - 1]["seed_sha"]:
            same_turn_pairs += 1
            msg1_same = call["seed_sha"] == calls[i - 1]["seed_sha"]
            if not msg1_same:
                mutations += 1
        if gap != 0 or error:
            parts_sum = sum(p["chars"] for p in parts)
            print(f"!! RECONCILIATION FAILED call={i} whole={whole} parts_sum={parts_sum} "
                  f"gap={gap}{'  · ' + error if error else ''}")
            invalid = invalid or (f"INVALID: call {i} parts do not sum to the whole "
                                  f"(gap={gap} chars){'; ' + error if error else ''}")
        for p in parts:
            p["est_tokens"] = _est_tokens(p["bytes"])
            agg = totals.setdefault(p["cat"], {"chars": 0, "bytes": 0, "est_tokens": 0})
            agg["chars"] += p["chars"]
            agg["bytes"] += p["bytes"]
            ragg = rollup.setdefault(_rollup_cat(p["cat"]), {"chars": 0, "bytes": 0, "est_tokens": 0})
            ragg["chars"] += p["chars"]
            ragg["bytes"] += p["bytes"]
            sagg = split[bucket]
            sagg[_rollup_cat(p["cat"])] = sagg.get(_rollup_cat(p["cat"]), 0) + p["chars"]
        rows.append({"call": i, "turn": turn, "first_of_turn": bool(first), "n_msgs": len(call["msgs"]),
                     "n_schemas": call["n_schemas"], "whole_chars": whole, "gap": gap,
                     "msg1_same_as_prev": msg1_same, "parts": parts})
    # CALIBRATED TOKEN ESTIMATE (2026-08-05 fix): the repo's static byte->token ratio is a RUNTIME
    # budgeting constant, not a billing one — measured against this very run it was 2.61
    # chars/token while the provider billed 4.26, inflating every est_tokens by ~1.63x. Shares
    # were unaffected (uniform scale) but est_tokens x price — the number an optimization
    # decision reads — was 63% too high. Calibrate from the run's own provider ledger.
    prompt_bytes = sum(a["bytes"] for a in totals.values())
    meter_in = int(res.get("in_total") or 0)
    calibrated = (meter_in / prompt_bytes) if (prompt_bytes and meter_in) else None
    ratio = calibrated if calibrated else _TPB
    estimator = ("calibrated(meter.in_total/prompt_bytes)" if calibrated
                 else f"UNCALIBRATED {_ESTIMATOR}")
    for agg in list(totals.values()) + list(rollup.values()):
        agg["est_tokens"] = int(round(agg["bytes"] * ratio))
    # SELF-CHECK: the categories must reconstruct the provider's own input total. This is the
    # alarm that would have caught the 1.63x drift the day it appeared.
    est_sum = sum(a["est_tokens"] for a in totals.values())
    drift = abs(est_sum - meter_in) / meter_in if meter_in else 0.0
    if meter_in and drift > 0.10:
        invalid = (invalid + " | " if invalid else "") + (
            f"CENSUS ESTIMATOR DRIFT: categories sum to {est_sum:,} est tokens but the provider "
            f"billed {meter_in:,} input tokens ({drift:.0%} off) — dollar attributions are not "
            "trustworthy")
    return {
        "label": label, "scenario": scenario, "estimator": estimator,
        "tokens_per_byte": round(ratio, 6), "static_tokens_per_byte": _TPB,
        "estimator_drift_vs_meter": round(drift, 4),
        "passed": res.get("passed"), "detail": res.get("detail"), "wall_s": round(wall_s, 1),
        "invalid": invalid, "calls": len(calls),
        "calls_outside_turn_map": max(0, len(calls) - len(turn_of)),
        "totals_by_category": totals, "rollup": rollup, "first_vs_later": split,
        "msg1_churn": {"same_turn_pairs": same_turn_pairs, "mutations": mutations},
        "meter": {k: res.get(k) for k in ("in_total", "in_cached", "in_fresh", "out_total",
                                          "peak_in", "cost_usd", "calls")},
        "rows": rows,
    }


def report(summary: dict, path: str) -> None:
    whole_total = sum(r["whole_chars"] for r in summary["rows"])
    gate = "OK" if not summary["invalid"] else "FAILED"
    print(f"[census {summary['label']} {summary['scenario']}] calls={summary['calls']} "
          f"whole={whole_total:,} chars · gate={gate} · est via {summary['estimator']}")
    print(f"  {'category':<44} {'chars':>12} {'est_tokens':>11} {'share':>7}")
    for cat, agg in sorted(summary["totals_by_category"].items(), key=lambda kv: -kv[1]["chars"]):
        share = agg["chars"] / max(whole_total, 1)
        print(f"  {cat:<44} {agg['chars']:>12,} {agg['est_tokens']:>11,} {share:>6.1%}")
    fc = sum(summary["first_vs_later"]["first_calls"].values())
    lc = sum(summary["first_vs_later"]["later_calls"].values())
    print(f"  first-call-of-turn total {fc:,} chars · later-call total {lc:,} chars")
    churn = summary["msg1_churn"]
    print(f"  same-turn msg1 mutations: {churn['mutations']} of {churn['same_turn_pairs']} pairs")
    if summary["invalid"]:
        print(f"  !! {summary['invalid']}")
    print(f"wrote {path}")


def run_census(scenario_obj: dict, *, label: str, out_dir: str) -> tuple[dict, str]:
    CAP["calls"] = []
    bench._Tap = _CensusTap
    t0 = time.time()
    res = bench.run(scenario_obj, memory_mode="real")
    summary = summarize(res, CAP["calls"], scenario=scenario_obj["name"], label=label,
                        wall_s=time.time() - t0)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{scenario_obj['name']}-{label}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False)
    report(summary, path)
    return summary, path


# ---------------------------------------------------------------------------------------------
# Selftest: drives bench.run with a scripted offline LLM (tests/test_benchmark_runner.py pattern)
# and asserts the reconciliation gate holds and the JSON lands. No API key, no spend.

class _ScriptedLLM:
    """Call 1 of each turn issues a read_file tool call; call 2 answers — so the census sees
    assistant tool_call envelopes AND tool-result messages, not just the seed."""

    def __init__(self):
        self.model = "fake-model"
        self.n = 0

    def set_cache_key(self, _key):
        return None

    def complete(self, _messages, _tools):
        from sliceagent.interfaces import AssistantMessage, ToolCall
        self.n += 1
        if self.n % 2 == 1:
            return AssistantMessage(
                content="", tool_calls=[ToolCall(id=f"c{self.n}", name="read_file",
                                                 args={"path": "sentinel.txt"})],
                finish_reason="tool_calls", usage={"prompt_tokens": 3, "completion_tokens": 1},
            )
        return AssistantMessage(content="done", tool_calls=[], finish_reason="stop",
                                usage={"prompt_tokens": 3, "completion_tokens": 1})


def _selftest_scenario() -> dict:
    def setup(root):
        with open(os.path.join(root, "sentinel.txt"), "w", encoding="utf-8") as stream:
            stream.write("ready")

    def verify(root):
        return os.path.isfile(os.path.join(root, "sentinel.txt")), "sentinel"

    return {"name": "lifecycle-probe", "meta": {"max_steps_per_turn": 4},
            "prompts": ["Initial stable task", "Follow-up detail"],
            "setup": setup, "verify": verify}


def selftest(out_dir: str) -> int:
    old_factory = bench._configured_llm
    try:
        bench._configured_llm = _ScriptedLLM
        summary, path = run_census(_selftest_scenario(), label="selftest", out_dir=out_dir)
    finally:
        bench._configured_llm = old_factory
    failures = []
    if summary["invalid"]:
        failures.append(f"reconciliation gate failed: {summary['invalid']}")
    if not summary["passed"]:
        failures.append(f"scripted scenario did not pass: {summary['detail']}")
    if not os.path.isfile(path):
        failures.append(f"census JSON not written: {path}")
    if summary["calls"] != 4:
        failures.append(f"expected 4 captured calls (2 turns x 2), got {summary['calls']}")
    totals = summary["totals_by_category"]
    for needed in ("system", "schemas", "seed:_pre_headers", "assistant_tool_calls", "tool_result"):
        if totals.get(needed, {}).get("chars", 0) <= 0:
            failures.append(f"category '{needed}' not observed — decomposition is not exercising it")
    if not any(cat.startswith("seed:") and cat not in ("seed:_pre_headers",)
               for cat in totals):
        failures.append("no seed region chunks attributed — header split dead")
    if summary["msg1_churn"]["same_turn_pairs"] != 2:
        failures.append(f"expected 2 same-turn msg1 pairs, got {summary['msg1_churn']['same_turn_pairs']}")
    if any(r["gap"] != 0 for r in summary["rows"]):
        failures.append("per-call gap nonzero")
    if failures:
        for f in failures:
            print(f"SELFTEST FAIL: {f}")
        return 1
    print("SELFTEST PASS: reconciliation gate exact on all 4 calls; JSON written; "
          "tool-call envelope, tool results, seed regions and msg1 churn all attributed")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exact serializer census at the local provider boundary.")
    ap.add_argument("--scenario", default="s2_taskdag_scheduler")
    ap.add_argument("--label", default=None, help="run label recorded in the output filename")
    ap.add_argument("--selftest", action="store_true",
                    help="validate the instrument offline with a scripted LLM (no API spend)")
    ap.add_argument("--out", default=os.path.join(ROOT, "evals", "census_runs"))
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest(args.out)
    if not args.label:
        ap.error("--label is required for a real run (or pass --selftest)")
    bench._Tap = _CensusTap
    scn = bench.load_scenario(args.scenario)
    summary, _path = run_census(scn, label=args.label, out_dir=args.out)
    return 1 if summary["invalid"] else 0


if __name__ == "__main__":
    sys.exit(main())
