"""HARDBENCH driver (STAGED) — a genuine multi-turn, cross-turn test.

Each scenario is an ordered chain of small requirements ("stages"). The human reveals exactly ONE per
turn, each building on the agent's previous work; nothing is revealed up front and the agent CANNOT finish
early — it works the current increment, then the human adds the next. The repo persists across turns, so
the agent must carry its prior work forward and ADAPT it when a later stage contradicts an earlier one.
Deterministic (the increments are fixed text — no LLM human-sim), so it's reproducible. Grade once at the
end with the hidden verifier (which checks the cumulative result, including the contradicting stages).

This is the cross-turn regime: sliceagent seals each turn and rebuilds a bounded slice for the next increment
(re-reading its own files as needed) vs codex re-sending the whole growing transcript.

Run (from repo root):
  sliceagent xhigh:  LLM_API_KEY=$OPENAI_API_KEY AGENT_MODEL=gpt-5.5 AGENT_REASONING=max \
                     PYTHONPATH=src:evals .venv/bin/python evals/hardbench/run.py sliceagent
  codex:           (unset OPENAI_API_KEY; set proxies) ... run.py codex
"""
import os
import sys
import json
import time
import shutil
import tempfile
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from swebench.bench2x2 import _UsageTap, InSessionCache   # noqa: E402
from colbench_trial import _llm                            # noqa: E402
from hardbench.scenarios import SCENARIOS                  # noqa: E402

MAX_STEPS = 30
MODEL = os.environ.get("AGENT_MODEL", "gpt-5.5")

PROTOCOL = (
    "You are an autonomous coding agent working in a REAL repository (the current working directory) with "
    "file and shell tools (read, edit, run commands, grep). You are collaborating with a human who will give "
    "you requirements ONE AT A TIME, across several turns — each new request builds on the work you did in "
    "earlier turns. For each request: use your tools to implement it in the repo, KEEP your earlier work and "
    "adapt it as needed (re-read your own files if you need to refresh your memory of them), and verify by "
    "running the tests or the tool. You do not need to ask what's next — the human will guide you turn by "
    "turn. Just do the current request well and make sure everything from earlier still works.\n\n"
    "First request: ")


def setup_repo(sc, root):
    for rel, content in sc["files"].items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p) or root, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)


def grade(sc, root):
    for rel, content in sc.get("hidden", {}).items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p) or root, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    # purge stale bytecode: an agent that ran tests then edited a .py within the same second could leave a
    # __pycache__/.pyc whose recorded mtime matches the new file, so Python would grade pre-edit code.
    for c in [os.path.join(dp, "__pycache__") for dp, _, _ in os.walk(root)
              if os.path.isdir(os.path.join(dp, "__pycache__"))]:
        shutil.rmtree(c, ignore_errors=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        r = subprocess.run(sc["verify"], cwd=root, capture_output=True, text=True, timeout=120, env=env)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return False, f"(verify error: {type(e).__name__}: {e})"


def run_sliceagent(name, sc):
    from sliceagent.pfc import slice_sink, record_user
    from sliceagent.seed import make_build_slice
    from sliceagent.text_utils import one_line
    from sliceagent.loop import run_turn
    from sliceagent.tools import LocalToolHost
    from sliceagent.code_index import make_code_index
    from sliceagent.events import AssistantText, ToolResult, make_dispatcher
    from sliceagent.hooks import BudgetHook, CatastrophicSafeguardHook, CompositeHooks
    from sliceagent.session import Session
    from sliceagent.hippocampus import make_episode_sink
    from sliceagent.hippocampus import make_search_history_tool

    repo = tempfile.mkdtemp(prefix=f"hb_{name}_mem_")
    setup_repo(sc, repo)
    memory = InSessionCache()
    session = Session(memory)
    tools = LocalToolHost(repo)
    retriever = make_code_index(repo)
    sid = session.session_id
    tools.registry.register(make_search_history_tool(memory, sid))
    episodic = make_episode_sink(memory, session_id=sid, task_id_fn=lambda: session.active_id or "t",
                                 title_fn=lambda: one_line(session.active().goal, 80) if session.active_id else "")
    last = {"text": ""}; ctr = {"tools": 0}

    def cap(e):
        if isinstance(e, AssistantText) and (e.content or "").strip():
            last["text"] = e.content.strip()
        elif isinstance(e, ToolResult):
            ctr["tools"] += 1
    build = make_build_slice(session, tools, retriever, memory, sc["stages"][0], session_id=sid)
    dispatch = make_dispatcher(slice_sink(session), episodic, cap)
    hooks = CompositeHooks(CatastrophicSafeguardHook(), BudgetHook(12_000_000))
    tap = _UsageTap(_llm()); tap.set_cache_key(sid)
    sess = []; peaks = []; steps_log = []; t0 = time.time()
    for i, stage in enumerate(sc["stages"]):
        msg = (PROTOCOL + stage) if i == 0 else ("[Human]: " + stage)
        if i == 0:
            session.new_topic(msg)
        else:
            session.continue_topic(msg)
        record_user(session.active(), msg)
        last["text"] = ""; tap.calls = []
        try:
            run_turn(build_slice=build, llm=tap, tools=tools, dispatch=dispatch, hooks=hooks, max_steps=MAX_STEPS)
        except Exception as e:  # noqa: BLE001
            last["text"] = f"(agent error: {type(e).__name__}: {e})"
        peaks.append(max((c["prompt"] for c in tap.calls), default=0))
        steps_log.append({"call_ins": [c["prompt"] for c in tap.calls],
                          "call_cached": [c.get("cached", 0) for c in tap.calls],
                          "out": sum(c["completion"] for c in tap.calls)})
        sess.extend(tap.calls)
        print(f"  [mem {name} stage{i+1}/{len(sc['stages'])}] calls={len(tap.calls)} "
              f"call_ins(k)={[c['prompt']//1000 for c in tap.calls]}", flush=True)
    passed, vout = grade(sc, repo)
    efficiency = tools.efficiency_metrics()
    shutil.rmtree(repo, ignore_errors=True)
    return {"task": name, "agent": "sliceagent", "passed": passed, "stages": len(sc["stages"]),
            "tools": ctr["tools"], "in_total": sum(c["prompt"] for c in sess),
            "in_cached": sum(c.get("cached", 0) for c in sess),
            "out_total": sum(c["completion"] for c in sess), "peak_in": max(peaks, default=0),
            "peaks": peaks, "steps": steps_log, "wall_s": round(time.time() - t0, 1), "verify_tail": vout[-400:],
            **efficiency}


def codex_turn(prompt, repo):
    cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write", "-C", repo, prompt]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return "(codex timeout)", {"in": 0, "out": 0}
    msg, u = "", {"in": 0, "out": 0, "cached": 0}
    for ln in proc.stdout.splitlines():
        try:
            o = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if o.get("type") == "item.completed":
            it = o.get("item", {})
            if it.get("type") == "agent_message" and it.get("text"):
                msg = it["text"]
        elif o.get("type") == "turn.completed":
            us = o.get("usage", {})
            u["in"] += us.get("input_tokens", 0); u["out"] += us.get("output_tokens", 0)
            u["cached"] += us.get("cached_input_tokens", 0)
    return (msg or "(no message)"), u


def run_codex(name, sc):
    repo = tempfile.mkdtemp(prefix=f"hb_{name}_cdx_")
    setup_repo(sc, repo)
    dialogue = ""; peaks = []; tin = tout = tcached = 0; t0 = time.time()
    for i, stage in enumerate(sc["stages"]):
        if i == 0:
            prompt = PROTOCOL + stage
        else:
            prompt = ("=== conversation so far ===\n" + dialogue
                      + "\n\n[Next request from the human]: " + stage
                      + "\n\nDo this next request in the repo, keeping your earlier work intact.")
        msg, u = codex_turn(prompt, repo)
        tin += u["in"]; tout += u["out"]; tcached += u["cached"]; peaks.append(u["in"])
        dialogue += f"\n[Human]: {stage}\n[Agent]: {msg}"
        print(f"  [codex {name} stage{i+1}/{len(sc['stages'])}] in={u['in']:,} cached={u['cached']:,} msg={msg[:50]!r}", flush=True)
    passed, vout = grade(sc, repo)
    shutil.rmtree(repo, ignore_errors=True)
    return {"task": name, "agent": "codex", "passed": passed, "stages": len(sc["stages"]),
            "tools": None, "in_total": tin, "in_cached": tcached, "out_total": tout,
            "peak_in": max(peaks, default=0), "peaks": peaks, "wall_s": round(time.time() - t0, 1),
            "verify_tail": vout[-400:]}


def main():
    agent = sys.argv[1] if len(sys.argv) > 1 else "sliceagent"
    only = sys.argv[2:] or list(SCENARIOS)
    runner = run_sliceagent if agent == "sliceagent" else run_codex
    out = os.environ.get("HB_OUT", f"/tmp/hardbench_{agent}.json")
    results = []
    for name in only:
        sc = SCENARIOS[name]
        print("=" * 80, flush=True); print(f"HARDBENCH {agent} — {name} ({sc['kind']}, {len(sc['stages'])} stages)", flush=True)
        rec = runner(name, sc)
        results.append(rec)
        print(f"  -> {'PASS' if rec['passed'] else 'FAIL'} | stages={rec['stages']} tools={rec['tools']} "
              f"tok={rec['in_total']+rec['out_total']:,} peak={rec['peak_in']:,} wall={rec['wall_s']}s", flush=True)
        json.dump(results, open(out, "w"), indent=2)
    npass = sum(1 for r in results if r["passed"])
    print(f"\nSUMMARY ({agent}): {npass}/{len(results)} passed -> {out}")


if __name__ == "__main__":
    main()
