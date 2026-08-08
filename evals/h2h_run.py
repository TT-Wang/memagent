"""Head-to-head benchmark: sliceagent (bounded slice) vs Kimi Code (growing transcript), SAME model
(K2.7 Code), SAME fresh workdir, SAME prompt(s), INDEPENDENT verifier.

Metrics (per the ask): token usage, steps, speed, accuracy. Plus per-call input series so we can see
context GROWTH (the thesis): a bounded slice should keep per-call input ~flat while a transcript grows.

Both agents are driven HEADLESS:
  - sliceagent: in-process run_turn, multi-turn on ONE persistent Slice (continuity = the slice carries
    findings/conversation/edited-files across turns; reset() is called ONCE).
  - Kimi Code: `kimi -p` (turn 1) then `kimi -r <session> -p` (later turns); usage read back from the
    session's wire.jsonl (usage.record entries: inputOther/inputCacheRead/inputCacheCreation/output).

Run (env must point at the SAME model both sides):
  cd ~/Desktop/sliceagent
  set -a; . "../agent design/.env"; set +a
  export LLM_API_KEY="$MOONSHOT_API_KEY" LLM_BASE_URL="https://api.moonshot.cn/v1" AGENT_MODEL=kimi-k2.7-code
  PYTHONPATH=src .venv/bin/python -m evals.h2h_run            # all scenarios, both agents
  PYTHONPATH=src .venv/bin/python -m evals.h2h_run --scenario s2_largefile_bug --agent kimi
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

H2H_DIR = os.path.dirname(os.path.abspath(__file__)) + "/h2h"
KIMI_BIN = os.path.expanduser("~/.kimi-code/bin/kimi")
KIMI_HOME = os.path.expanduser("~/.kimi-code")
KIMI_TURN_TIMEOUT = 600  # seconds per headless turn (a turn past this is wedged, not slow)


# ----------------------------------------------------------------------------- scenario loading
def _load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_scenario(name: str) -> dict:
    d = os.path.join(H2H_DIR, name)
    meta = json.load(open(os.path.join(d, "meta.json")))
    prompts = json.load(open(os.path.join(d, "prompts.json")))
    setup = _load_module(os.path.join(d, "setup.py"), f"{name}_setup").setup
    verify = _load_module(os.path.join(d, "verify.py"), f"{name}_verify").verify
    return {"name": name, "dir": d, "meta": meta, "prompts": prompts, "setup": setup, "verify": verify}


def all_scenarios() -> list[str]:
    return sorted(n for n in os.listdir(H2H_DIR)
                  if os.path.isdir(os.path.join(H2H_DIR, n)) and
                  os.path.exists(os.path.join(H2H_DIR, n, "meta.json")))


# ----------------------------------------------------------------------------- sliceagent driver
class _UsageTap:
    """Wrap the LLM so every model call's usage AND latency is captured -> per-call series, totals,
    and a STALL-EXCLUDED wall (the flaky Moonshot endpoint occasionally stalls a call to the ~75s
    hard-timeout; those infra stalls would otherwise pollute the wall-time metric)."""
    def __init__(self, inner):
        self.inner = inner
        self.calls = []       # [{prompt, completion, cached}]
        self.latencies = []   # per-call wall seconds (incl. retries)

    def complete(self, messages, tools):
        t0 = time.time()
        r = self.inner.complete(messages, tools)
        self.latencies.append(time.time() - t0)
        u = (r.usage or {}) if hasattr(r, "usage") else {}
        self.calls.append({"prompt": u.get("prompt_tokens", 0),
                           "completion": u.get("completion_tokens", 0),
                           "cached": u.get("cached_tokens", 0)})
        return r


def run_sliceagent(scn: dict, workdir: str, model: str, *, enable_kernel_tools: bool = False, llm=None) -> dict:
    from sliceagent.pfc import Slice, slice_sink, record_user
    from sliceagent.seed import make_build_slice
    from sliceagent.loop import run_turn
    from sliceagent.tools import LocalToolHost
    from sliceagent.code_index import make_code_index
    from sliceagent.memory import NullMemory
    from sliceagent.retriever import NullRetriever
    from sliceagent.events import ToolResult, make_dispatcher
    from sliceagent.telemetry import make_telemetry_sink
    from sliceagent.llm import OpenAILLM

    meta, prompts = scn["meta"], scn["prompts"]
    max_steps = int(meta.get("max_steps_per_turn", 20))

    state = Slice()
    state.reset(prompts[0])
    tools = LocalToolHost(root=workdir)
    # PRODUCTION ACTIVE-WORK PARITY (same defect contextbench_run.py carried until the cost review,
    # fixed there 2026-08-05): with no bound provider, LocalToolHost.schemas() keeps the six legacy
    # semantic-state tools AND injects the 351-char `note` arg into EVERY schema — 12,697 chars/call
    # the CLI never pays. Binding alone is schema reduction, not parity, so each turn is admitted with
    # stable ids too: without a request ROOT, update_work stays advertised in a state where it errors.
    _cur_logical = {"id": "L1"}
    if callable(getattr(tools, "bind_active_work", None)):
        tools.bind_active_work(lambda: (state.active_work, _cur_logical["id"], 0))
    # (pin/view active-asker tools were retired with rebuild mode; enable_kernel_tools is now a no-op.)
    retriever = make_code_index(workdir) if meta.get("use_code_index") else NullRetriever()
    tel = make_telemetry_sink()
    pv = {"pin": 0, "view": 0}   # instrument: did the model actually USE pin/view?
    def _pv_sink(e):
        if isinstance(e, ToolResult) and getattr(e, "name", None) in pv:
            pv[e.name] += 1
    dispatch = make_dispatcher(slice_sink(state), tel, _pv_sink)
    # Tight per-call timeout so a transient stuck connection ABORTS and retries (sliceagent has
    # jittered-backoff retry) instead of hanging the whole run for minutes on one wedged socket.
    _base_llm = llm if llm is not None else OpenAILLM(model=model, timeout=60.0)
    if hasattr(_base_llm, "set_cache_key"):     # per-cell stable prompt-cache routing (Fix 1)
        _base_llm.set_cache_key(os.path.basename(workdir))
    tap = _UsageTap(_base_llm)
    memory = NullMemory()

    # BREADTH wiring (opt-in via meta flags) — real-case breadth scenarios configure the periphery so the
    # LIVE model can actually USE skills / plugins / subagents / MCP. Mirrors cli.py's wiring on the same
    # registry/skills seams. Absent for ordinary depth scenarios (no flags) → those run exactly as before.
    skills_mgr = None
    if meta.get("skills") or meta.get("plugins"):
        from sliceagent.skills import make_skill_manager
        skills_mgr = make_skill_manager([os.path.join(workdir, ".sliceagent", "skills"), workdir])
    if meta.get("plugins"):
        from sliceagent.plugins import load_plugins
        os.environ["AGENT_ALLOW_PLUGINS"] = "1"   # the scenario's own plugin is trusted by construction
        try:
            load_plugins(tools.registry, skills_mgr, [os.path.join(workdir, ".sliceagent", "plugins")],
                         root=workdir, config=None)
        except Exception as _e:  # a broken plugin must not abort the scenario run
            print(f"  · plugin load error ({scn['name']}): {_e}")
    if skills_mgr is not None:
        from sliceagent.skills import make_skill_tool
        _se = make_skill_tool(skills_mgr)
        if _se is not None:
            tools.registry.register(_se)
    if meta.get("mcp"):
        from sliceagent.mcp_client import connect_mcp_servers
        # meta.json is static but the workdir is per-run: force the venv python (so the stub server has the
        # mcp SDK) and cwd=workdir (so a relative server path written by setup() resolves). The scenario's
        # setup() writes the stub server into the workdir; meta.mcp gives its relative args.
        servers = {n: {**c, "command": sys.executable, "cwd": workdir} for n, c in meta["mcp"].items()}
        try:
            connect_mcp_servers(tools.registry, servers, on_log=lambda m: None)
        except Exception as _e:
            print(f"  · mcp connect error ({scn['name']}): {_e}")
    if meta.get("subagents"):
        from sliceagent.scoped_spawn import ScopedSpawnHost
        tools = ScopedSpawnHost(tools, llm=tap, retriever=retriever, memory=memory)

    per_turn = []
    t0 = time.time()
    err = ""
    try:
        for i, p in enumerate(prompts):
            if i > 0:
                state.goal = p  # new turn's task (do NOT reset — that wipes continuity)
            lid = f"L{i + 1}"
            _cur_logical["id"] = lid
            record_user(state, p, source_artifact=f"turn-{i + 1:03d}", source_event_id=f"ev-{i + 1:03d}",
                        source_text=p, logical_id=lid, workspace_epoch=0)
            build = make_build_slice(state, tools, retriever, memory, p)
            n_before = len(tap.calls)
            res = run_turn(build_slice=build, llm=tap, tools=tools, dispatch=dispatch, max_steps=max_steps)
            calls_turn = tap.calls[n_before:]
            per_turn.append({"turn": i + 1, "steps": res.steps, "stop": res.stop_reason,
                             "in": sum(c["prompt"] for c in calls_turn),
                             "out": sum(c["completion"] for c in calls_turn),
                             "peak_in": max((c["prompt"] for c in calls_turn), default=0)})
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"

    wall = time.time() - t0
    passed, detail = (False, err) if err else scn["verify"](workdir)
    calls = tap.calls
    tm = tel.summary()
    STALL = 65.0  # a call at/above this hit the ~75s hard-timeout backstop = infra stall, not real work
    stalls = sum(1 for x in tap.latencies if x >= STALL)
    wall_clean = sum(x for x in tap.latencies if x < STALL)
    return {
        "agent": "sliceagent", "scenario": scn["name"], "passed": bool(passed), "detail": str(detail)[:80],
        "steps": len(calls), "wall_s": round(wall, 1),
        "wall_clean": round(wall_clean, 1),  # wall excluding infra stalls (fair vs Kimi's stall-free baseline)
        "stalls": stalls,
        "in_total": sum(c["prompt"] for c in calls), "in_cached": sum(c["cached"] for c in calls),
        "out_total": sum(c["completion"] for c in calls),
        "peak_in": max((c["prompt"] for c in calls), default=0),
        "series_in": [c["prompt"] for c in calls], "per_turn": per_turn,
        "re_reads": tm.get("re_reads", 0), "recalls": tm.get("recalls", 0),
        "pinview_calls": pv,   # did the model actually invoke the active-asker syscalls?
    }


# ----------------------------------------------------------------------------- kimi driver
def _kimi_session_dir(session_id: str) -> str | None:
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


def _kimi_usage(session_dir: str) -> dict:
    """Sum usage.record across ALL agents (main + any subagents) in the session. Each record is one
    model call: usage={inputOther, inputCacheRead, inputCacheCreation, output}."""
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

    def call_in(u):  # total input tokens this call (fresh + cache-read + cache-creation)
        return u.get("inputOther", 0) + u.get("inputCacheRead", 0) + u.get("inputCacheCreation", 0)

    in_total = sum(call_in(r["usage"]) for r in recs)
    in_cached = sum(r["usage"].get("inputCacheRead", 0) for r in recs)
    out_total = sum(r["usage"].get("output", 0) for r in recs)
    peak_in = max((call_in(r["usage"]) for r in recs), default=0)
    model = recs[-1].get("model", "?") if recs else "?"
    return {"calls": len(recs), "in_total": in_total, "in_cached": in_cached, "out_total": out_total,
            "peak_in": peak_in, "series_in": [call_in(r["usage"]) for r in recs], "model": model}


def run_kimi(scn: dict, workdir: str) -> dict:
    prompts = scn["prompts"]
    session_id = None
    per_turn = []
    t0 = time.time()
    err = ""
    for i, p in enumerate(prompts):
        cmd = [KIMI_BIN] + (["-r", session_id] if session_id else []) + \
              ["-p", p, "--output-format", "stream-json"]
        tt = time.time()
        try:
            proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                                  timeout=KIMI_TURN_TIMEOUT)
        except subprocess.TimeoutExpired:
            err = f"turn {i+1} timed out after {KIMI_TURN_TIMEOUT}s"
            per_turn.append({"turn": i + 1, "wall": KIMI_TURN_TIMEOUT, "rc": "timeout"})
            break
        dt = time.time() - tt
        for ln in proc.stdout.splitlines():
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if o.get("type") == "session.resume_hint" or o.get("role") == "meta":
                session_id = o.get("session_id", session_id)
        per_turn.append({"turn": i + 1, "wall": round(dt, 1), "rc": proc.returncode,
                         "stderr_tail": proc.stderr.strip()[-160:]})
        if proc.returncode != 0 and session_id is None:
            err = f"turn {i+1} rc={proc.returncode}: {proc.stderr.strip()[-160:]}"
            break
        if session_id is None:
            err = f"turn {i+1}: no session id in output"
            break
    wall = time.time() - t0
    # Same infra-stall exclusion the sliceagent arm applies (review M12): a per-turn wall at/above
    # the ~75s hard-timeout backstop is an endpoint stall, not real work, and the moat comparison
    # must not count it against only one arm. kimi's per-turn walls are already recorded above.
    STALL = 65.0
    clean_walls = [p["wall"] for p in per_turn if p["wall"] < STALL]
    wall_clean = round(sum(clean_walls), 1)

    passed, detail = (False, err) if err else scn["verify"](workdir)
    sess_dir = _kimi_session_dir(session_id) if session_id else None
    u = _kimi_usage(sess_dir) if sess_dir else {"calls": 0, "in_total": 0, "in_cached": 0,
                                                "out_total": 0, "peak_in": 0, "series_in": [], "model": "?"}
    return {
        "agent": "kimi", "scenario": scn["name"], "passed": bool(passed), "detail": str(detail)[:80],
        "steps": u["calls"], "wall_s": round(wall, 1),
        "wall_clean": wall_clean, "stalls": len(per_turn) - len(clean_walls),
        "in_total": u["in_total"], "in_cached": u["in_cached"], "out_total": u["out_total"],
        "peak_in": u["peak_in"], "series_in": u["series_in"], "per_turn": per_turn,
        "kimi_model": u["model"], "session_id": session_id,
    }


# ----------------------------------------------------------------------------- cost (the moat constraint)
# Per-model token prices ($/Mtok): (cache-miss input, cache-hit input, output). Default = kimi-k2.7-code,
# the model both agents run, so $ is the apples-to-apples cost the moat must beat. Keyed loosely on model id.
PRICES = {
    "kimi-k2.7-code": (0.95, 0.19, 4.00),
    "default": (0.95, 0.19, 4.00),
}


def _price(model: str) -> tuple:
    m = (model or "").lower()
    for k, v in PRICES.items():
        if k != "default" and k in m:
            return v
    return PRICES["default"]


def cost_usd(r: dict, model: str = "kimi-k2.7-code") -> float:
    """Dollar cost of a run, cache-aware (cached input billed at the hit rate). This is the moat's
    load-bearing number: sliceagent MUST stay below kimi here, not just on raw tokens."""
    p_miss, p_hit, p_out = _price(model)
    fresh = max(r.get("in_total", 0) - r.get("in_cached", 0), 0)
    return (fresh * p_miss + r.get("in_cached", 0) * p_hit + r.get("out_total", 0) * p_out) / 1e6


# ----------------------------------------------------------------------------- orchestration
def _row(r: dict) -> str:
    tot = r["in_total"] + r["out_total"]
    fresh = r["in_total"] - r["in_cached"]
    cache_pct = 100 * r["in_cached"] / max(r["in_total"], 1)
    wall = r.get("wall_clean", r["wall_s"])
    st = r.get("stalls", 0)
    wtag = f"{wall:>7.1f}s" + (f"(+{st}st)" if st else "       ")
    return (f"{r['scenario']:24} {r['agent']:9} {'PASS' if r['passed'] else 'FAIL':4} "
            f"{r['steps']:>4} {wtag}  tok={tot:>9}  in={r['in_total']:>9} "
            f"(fresh {fresh:>8}) cache={cache_pct:>4.0f}%  out={r['out_total']:>7}  peak={r['peak_in']:>7}  "
            f"${cost_usd(r):.4f}  {r['detail'][:26]}")


def _moat_summary(results: list, model: str) -> str:
    """Per-scenario sliceagent-vs-kimi comparison on the moat constraint ($ + peak ctx) and accuracy.
    Flags any scenario where sliceagent is NOT cheaper than kimi — a moat-constraint violation."""
    by = {}
    for r in results:
        by.setdefault(r.get("scenario"), {})[r.get("agent")] = r
    lines = ["", "=" * 92, "MOAT CONSTRAINT CHECK (sliceagent must be $-cheaper & lower-peak than kimi)", "=" * 92,
             f"{'scenario':26} {'mem $':>9} {'kimi $':>9} {'$ ratio':>8} {'mem peak':>9} {'kimi peak':>9} {'acc m/k':>8}"]
    viol = []
    for scn, d in by.items():
        m, k = d.get("sliceagent"), d.get("kimi")
        if not (m and k):
            continue
        mc, kc = cost_usd(m, model), cost_usd(k, model)
        ratio = kc / mc if mc else float("inf")
        flag = "" if mc <= kc else "  ⚠ MEM COSTLIER"
        if mc > kc:
            viol.append(scn)
        lines.append(f"{scn:26} {mc:>9.4f} {kc:>9.4f} {ratio:>7.1f}x {m['peak_in']:>9} {k['peak_in']:>9} "
                     f"{int(bool(m['passed']))}/{int(bool(k['passed']))}{flag}")
    lines.append("-" * 92)
    lines.append(f"moat constraint: {'HELD on all scenarios' if not viol else 'VIOLATED on: ' + ', '.join(viol)}")
    return "\n".join(lines)


def header() -> str:
    return (f"{'scenario':24} {'agent':9} {'acc':4} {'step':>4} {'wall':>8}  {'total_tok':>13}  "
            f"{'input':>12} {'(uncached)':>14} {'output':>11}  {'peak_ctx':>8}  detail")


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    # everyone uses the 3.13 venv python (the one running THIS file) for subprocess `python3`
    os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")

    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=None, help="one scenario name (default: all)")
    ap.add_argument("--agent", choices=["sliceagent", "kimi", "both"], default="both")
    ap.add_argument("--model", default=os.environ.get("AGENT_MODEL", "kimi-k2.7-code"))
    ap.add_argument("--out", default=os.path.join(H2H_DIR, "results.json"))
    args = ap.parse_args()

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
    from sliceagent.cli import _load_env
    _load_env()

    scenarios = [args.scenario] if args.scenario else all_scenarios()
    agents = ["sliceagent", "kimi"] if args.agent == "both" else [args.agent]

    # RESUMABLE + INCREMENTAL: load any prior results, skip done cells, persist after EACH cell so an
    # unattended crash/hang recovery never redoes finished work (just relaunch the same command).
    results = []
    if os.path.exists(args.out):
        try:
            results = json.load(open(args.out))
        except Exception:
            results = []
    done = {(r.get("scenario"), r.get("agent")) for r in results}

    print(f"head-to-head · model={args.model} · scenarios={scenarios} · agents={agents} · "
          f"resuming with {len(done)} cell(s) done\n")
    print(header())
    print("-" * 150)

    # Transient endpoint degradation (Moonshot throttling) shows up as timeouts/connection errors, not
    # agent logic failures. Retry a cell that fails PURELY on infra so a passing window isn't burned;
    # a clean PASS or a real (verifier) FAIL breaks immediately and is recorded.
    INFRA = ("timed out", "timeout", "connection", "apierror", "api error", "temporarily",
             "rate limit", "ratelimit", " 429", " 502", " 503", " 504", "overloaded")

    def is_infra_fail(r: dict) -> bool:
        return (not r.get("passed")) and any(m in str(r.get("detail", "")).lower() for m in INFRA)

    for sname in scenarios:
        scn = load_scenario(sname)
        for ag in agents:
            if (sname, ag) in done:
                print(f"{sname:24} {ag:9} SKIP (already in results.json)", flush=True)
                continue
            r = None
            for attempt in range(3):
                workdir = tempfile.mkdtemp(prefix=f"h2h-{sname}-{ag}-")
                scn["setup"](workdir)
                try:
                    r = run_sliceagent(scn, workdir, args.model) if ag == "sliceagent" else run_kimi(scn, workdir)
                except Exception as e:  # noqa: BLE001
                    r = {"agent": ag, "scenario": sname, "passed": False, "detail": f"runner: {e}",
                         "steps": 0, "wall_s": 0.0, "in_total": 0, "in_cached": 0, "out_total": 0,
                         "peak_in": 0, "series_in": [], "per_turn": []}
                r["workdir"] = workdir
                if not is_infra_fail(r):
                    break
                print(f"{sname:24} {ag:9} infra-fail attempt {attempt+1}/3 ({r['detail'][:40]}) — retrying",
                      flush=True)
            results.append(r)
            json.dump(results, open(args.out, "w"), indent=2)  # persist immediately (crash-safe)
            print(_row(r), flush=True)

    json.dump(results, open(args.out, "w"), indent=2)
    if any(r.get("agent") == "kimi" for r in results) and any(r.get("agent") == "sliceagent" for r in results):
        print(_moat_summary(results, args.model), flush=True)
    print(f"\nwrote {args.out}  ({len(results)} cells)")


if __name__ == "__main__":
    main()
