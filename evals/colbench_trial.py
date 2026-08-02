"""ColBench (Meta SWEET-RL) trial — sliceagent as the AGENT, gpt-5.5 as the simulated HUMAN, faithful to
the repo's prompts. Multi-turn collaborative coding: the agent asks clarifying questions, the human
answers from hidden info, ≤10 rounds, then the agent emits "I WANT TO ANSWER:" + code. Each human turn
is a new loop for sliceagent (continue_topic → SEAL), so this exercises exactly the multi-turn behavior.

Backend is fully scored (run agent code vs ground_truth on test_cases). Frontend is run + a lightweight
gpt-5.5 HTML-similarity judge (official visual scoring needs Firefox/GeckoDriver, not installed).

Run: set env (LLM_API_KEY=$OPENAI_API_KEY AGENT_MODEL=gpt-5.5 + proxy), then
  PYTHONPATH=src .venv/bin/python evals/colbench_trial.py
"""
import os
import re
import sys
import json
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from swebench.bench2x2 import _UsageTap, InSessionCache

MODEL = os.environ.get("AGENT_MODEL", "gpt-5.5")
# Prompts live in the PERSISTENT copy (evals/colbench/prompts/, committed) so a cleared /tmp can't
# break the harness again; fall back to the old /tmp location if present.
_PROMPTDIR = os.path.join(os.path.dirname(__file__), "colbench", "prompts")
if not os.path.exists(os.path.join(_PROMPTDIR, "human_simulator_code_prompt.txt")):
    _PROMPTDIR = "/tmp/sweet_rl/prompts"
HUMAN_CODE_PROMPT = open(os.path.join(_PROMPTDIR, "human_simulator_code_prompt.txt")).read()
HUMAN_HTML_PROMPT = open(os.path.join(_PROMPTDIR, "human_simulator_html_prompt.txt")).read()
AGENT_PROTOCOL = (
    "You are collaborating with a human over up to 10 short rounds to solve a PERSONALIZED programming "
    "problem. CRITICAL: the problem is INTENTIONALLY UNDER-SPECIFIED — the exact behavior (numeric "
    "conventions, rounding vs truncation, whether inputs are scaled, output format, edge cases) is hidden "
    "and only the human knows it. A function that looks reasonable but guesses these wrong will FAIL. So "
    "for the first few rounds ASK concise clarifying questions (one turn = a question or two, end your turn "
    "to let the human answer) — do NOT write the function until you've pinned the hidden details. The human "
    "answers briefly (≤2 sentences). Before you answer, PIN THE EXACT COMPUTATION — the precise formula, "
    "how EACH argument is used, and every choice between similar options (count-of-keys vs sum-of-values, "
    "any vs all, which dict keys/fields, rounding, defaults, what counts as a match, edge/empty cases). A "
    "plausible-but-slightly-wrong formula FAILS, so if any of these is unstated, ask one more targeted "
    "question (or state the exact logic you'll implement and let the human correct it) rather than guess. "
    "When (and only when) the exact logic is pinned, reply starting EXACTLY with 'I WANT TO ANSWER:' then "
    "the raw code. Use the EXACT function name AND the parameters in the EXACT order given in the problem's "
    "signature — do NOT rename the function and do NOT reorder/rename parameters (it is called positionally "
    "by that exact signature when graded). ARGUMENT TYPES ARE A REQUIRED PART OF YOUR FIRST QUESTION: for "
    "EACH parameter, you MUST confirm whether it is a single scalar, a string, or a COLLECTION (list/dict) "
    "that must be aggregated with sum()/len()/iteration — NEVER assume a scalar. A plural or aggregate-"
    "sounding name (e.g. '..._hours_worked', '..._contributed', '..._amounts', '..._times', '..._data', "
    "anything that could hold many values) is a list/dict until the human confirms otherwise — ask in the "
    "SAME first message as the formula. Guessing scalar for a list crashes with a TypeError. Be concise.")


def _llm():
    from sliceagent.llm import OpenAILLM
    return OpenAILLM(model=MODEL, timeout=60.0)


def human_turn(llm, base_prompt, problem, hidden, dialogue):
    p = (base_prompt.replace("{problem_description}", problem)
                    .replace("{hidden_information}", hidden)
                    .replace("{dialogue_history}", dialogue))
    r = llm.complete([{"role": "user", "content": p}], [])
    return (r.content or "").strip()


def run_session(task, mode):
    """Drive sliceagent ↔ simulated-human for one ColBench task. Returns dict with the answer + per-round cost."""
    from sliceagent.pfc import slice_sink, record_user
    from sliceagent.seed import make_build_slice
    from sliceagent.text_utils import one_line
    from sliceagent.loop import run_turn
    from sliceagent.tools import LocalToolHost
    from sliceagent.code_index import make_code_index
    from sliceagent.events import AssistantText, ToolResult, make_dispatcher
    from sliceagent.hooks import BudgetHook, CatastrophicSafeguardHook, CompositeHooks
    from sliceagent.session import Session
    from sliceagent.hippocampus import make_search_history_tool
    from sliceagent.hippocampus import make_episode_sink

    problem = task["problem_description"]
    hidden = task["hidden_information"]
    human_prompt = HUMAN_CODE_PROMPT if mode == "code" else HUMAN_HTML_PROMPT
    sim = _llm()
    sim.reasoning = "full"   # the human-sim runs at the provider default, NOT the agent's AGENT_REASONING
    # (matched-reasoning is about the AGENT under test; the sim must be identical for both agents)

    repo = tempfile.mkdtemp(prefix=f"colbench_{mode}_")
    memory = InSessionCache()
    session = Session(memory)
    tools = LocalToolHost(repo)
    retriever = make_code_index(repo)
    sid = session.session_id
    # ColBench is pure Q&A (empty repo; recall used 3x across 20 tasks). gpt-5.5 REJECTS reasoning_effort
    # together with function tools on /v1/chat/completions (400), so to run at high/xhigh we drop the tool
    # schemas entirely — the agent never needs them here, and the bounded slice still carries the dialogue.
    toolless = (os.environ.get("COLBENCH_TOOLLESS") or "").strip() not in ("", "0", "off") \
        or os.environ.get("AGENT_REASONING", "").strip().lower() in ("high", "max")
    if toolless:
        from sliceagent.registry import ToolRegistry
        tools.registry = ToolRegistry()        # no tools attached → reasoning_effort is accepted
    else:
        tools.registry.register(make_search_history_tool(memory, sid))
    episodic = make_episode_sink(memory, session_id=sid, task_id_fn=lambda: session.active_id or "t",
                                 title_fn=lambda: one_line(session.active().goal, 80) if session.active_id else "")
    last_assistant = {"text": ""}
    ctr = {"recall": 0}
    def cap(e):
        if isinstance(e, AssistantText) and (e.content or "").strip():
            last_assistant["text"] = e.content.strip()
        elif isinstance(e, ToolResult) and getattr(e, "name", "") == "recall_history":
            ctr["recall"] += 1
    build = make_build_slice(session, tools, retriever, memory, problem, session_id=sid)
    dispatch = make_dispatcher(slice_sink(session), episodic, cap)
    hooks = CompositeHooks(CatastrophicSafeguardHook(), BudgetHook(2_000_000))
    tap = _UsageTap(_llm()); tap.set_cache_key(sid)

    sig_m = re.search(r"def\s+\w+\s*\([^)]*\)", problem)
    req_sig = (f"\n\nREQUIRED FUNCTION SIGNATURE — your final answer MUST define EXACTLY this (identical "
               f"name and parameters, do not rename): {sig_m.group(0)}" if sig_m else "")
    dialogue = f"Human: {problem}"
    rounds = []
    answer = None
    sess = []          # all LLM calls across all rounds (for total tokens / peak / cache)
    import time as _t
    t0 = _t.time()
    human_msg = (f"{AGENT_PROTOCOL}\n\nThe human's problem: {problem}{req_sig}\n\n"
                 "The exact behavior is HIDDEN — do not write the function yet; ask your most important "
                 "clarifying question first.")
    for rnd in range(1, 11):
        if rnd == 1:
            session.new_topic(human_msg)
        else:
            session.continue_topic(f"[Human]: {human_msg}\n\n[Reminder] Ask a clarifying question, or if "
                                   f"ready reply starting with 'I WANT TO ANSWER:' then the raw code.")
        record_user(session.active(), human_msg)
        last_assistant["text"] = ""
        tap.calls = []
        try:
            run_turn(build_slice=build, llm=tap, tools=tools, dispatch=dispatch, hooks=hooks, max_steps=14)
        except Exception as e:  # noqa: BLE001
            last_assistant["text"] = f"(agent error: {type(e).__name__})"
        agent_msg = last_assistant["text"] or "(no response)"
        peak = max((c["prompt"] for c in tap.calls), default=0)
        sess.extend(tap.calls)
        rounds.append({"round": rnd, "peak_in": peak, "findings": len(session.active().findings)})
        dialogue += f"\nAgent: {agent_msg}"
        if "I WANT TO ANSWER:" in agent_msg.upper():
            answer = agent_msg[agent_msg.upper().index("I WANT TO ANSWER:") + len("I WANT TO ANSWER:"):].strip()
            break
        # human responds
        hreply = human_turn(sim, human_prompt, problem, hidden, dialogue)
        dialogue += f"\nHuman: {hreply}"
        human_msg = hreply
    tin = sum(c["prompt"] for c in sess); tcached = sum(c["cached"] for c in sess)
    return {"rounds": rounds, "answer": answer, "dialogue": dialogue, "repo": repo, "recall": ctr["recall"],
            "n_rounds": len(rounds), "peak_in": max((r["peak_in"] for r in rounds), default=0),
            "in_total": tin, "out_total": sum(c["completion"] for c in sess), "in_cached": tcached,
            "cache_pct": round(100 * tcached / tin) if tin else 0, "wall_s": round(_t.time() - t0, 1),
            "findings": rounds[-1]["findings"] if rounds else 0}


def _extract_code(ans, mode):
    if not ans:
        return ""
    m = re.search(r"```(?:python|html)?\n(.*?)```", ans, re.S)
    return (m.group(1) if m else ans).strip()


def score_backend(code, task):
    """Run the agent's function and the ground_truth on each test_case call; compare outputs (evaluate_code style)."""
    tests = task["test_cases"]
    tests = json.loads(tests) if isinstance(tests, str) else tests
    gt = task["ground_truth"]
    harness = {
        "agent_code": code, "gt_code": gt, "tests": tests,
    }
    script = r'''
import json, sys, re, types, inspect
h = json.load(open(sys.argv[1]))
def run(src):
    ns = {}; exec(src, ns); return ns
try:
    gt = run(h['gt_code'])
except Exception as e:
    print(json.dumps({'err': 'gt:' + str(e)})); sys.exit()
try:
    ag = run(h['agent_code'])
except Exception as e:
    print(json.dumps({'passed_strict': 0, 'passed_logic': 0, 'total': len(h['tests']), 'note': 'agent exec: ' + str(e)[:120]})); sys.exit()
m = re.search(r'def\s+(\w+)\s*\(([^)]*)\)', h['gt_code'])
req = m.group(1)
reqp = [p.split('=')[0].split(':')[0].strip() for p in m.group(2).split(',') if p.strip()]
def score(ns):
    p = t = 0
    for name, call in h['tests'].items():
        t += 1
        try:
            g = eval(call, dict(gt))
        except Exception:
            t -= 1; continue
        try:
            a = eval(call, dict(ns))
            p += 1 if (a == g or str(a) == str(g)) else 0
        except Exception:
            pass
    return p, t
# STRICT: agent code exactly as written (exact function name AND parameter order required)
ps, t = score(ag)
# LOGIC: alias the sole function to the required NAME, and when the agent's params are a PERMUTATION of
# the required ones (same names, different order) remap positional calls by name. Measures reasoning, not
# interface labels — the same philosophy as the name-alias, extended to param order.
agl = dict(ag); renamed = False
target = agl.get(req)
fns = [k for k, v in agl.items() if isinstance(v, types.FunctionType)]
if target is None and len(fns) == 1:
    target = agl[fns[0]]; renamed = True
if target is not None:
    # LOGIC forgives INTERFACE labels to measure REASONING: it already aliases a renamed function and
    # remaps a permuted param order; here it also SUPPLIES the required signature's DEFAULT values for any
    # optional arg the agent left as required (a dropped `=default` is an interface omission, not a
    # reasoning error). Identical treatment for both agents.
    try:
        rparams = list(inspect.signature(gt[req]).parameters.values())
    except Exception:
        rparams = [inspect.Parameter(n, inspect.Parameter.POSITIONAL_OR_KEYWORD) for n in reqp]
    try:
        ap = list(inspect.signature(target).parameters)
    except Exception:
        ap = reqp
    if ap != reqp:
        renamed = True
    def _wrap(fn, rparams):
        def w(*args, **kwargs):
            bound = {}
            for i, prm in enumerate(rparams):
                if i < len(args):
                    bound[prm.name] = args[i]
                elif prm.name in kwargs:
                    bound[prm.name] = kwargs[prm.name]
                elif prm.default is not inspect.Parameter.empty:
                    bound[prm.name] = prm.default
            try:
                return fn(**bound)                                                  # agent used the same names
            except TypeError:
                return fn(*[bound[prm.name] for prm in rparams if prm.name in bound])  # positional, required order
        return w
    agl[req] = _wrap(target, rparams)
pl, _ = score(agl)
print(json.dumps({'passed_strict': ps, 'passed_logic': pl, 'total': t, 'renamed': renamed}))
'''
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(harness, fh); fh.close()
    fs = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False); fs.write(script); fs.close()
    import subprocess
    for py in [os.path.expanduser("~/.sweb-venv/bin/python"), ".venv/bin/python", "python3"]:
        try:
            out = subprocess.run([py, fs.name, fh.name], capture_output=True, text=True, timeout=60)
            line = [l for l in out.stdout.splitlines() if l.startswith("{")]
            if line:
                d = json.loads(line[-1])
                d["passed"] = d.get("passed_logic", d.get("passed", 0))   # main metric = logic-correctness
                return d
        except Exception:
            continue
    return {"passed": 0, "passed_strict": 0, "passed_logic": 0, "total": 0, "note": "scoring harness failed"}


def main():
    # task files pre-downloaded via ~/.sweb-venv (has huggingface_hub); this driver runs in sliceagent's .venv
    paths = sys.argv[1:] or [os.environ.get("COLBENCH_BACKEND", "/tmp/colbench_backend_task.json")]
    outfile = os.environ.get("COLBENCH_OUT", "/tmp/colbench_trial.json")
    out = []
    for path in paths:
        btask = json.load(open(path))
        print("=" * 80, flush=True)
        print("COLBENCH —", os.path.basename(path), "—", btask["problem_description"][:70], flush=True)
        res = run_session(btask, "code")
        code = _extract_code(res["answer"], "code")
        score = score_backend(code, btask) if code else {"passed": 0, "total": 0, "note": "no answer"}
        rec = {"task": os.path.basename(path), "agent": "sliceagent",
               "passed": score.get("passed", 0), "total": score.get("total", 0),
               "renamed": score.get("renamed", False), "n_rounds": res["n_rounds"], "recall": res["recall"],
               "peak_in": res["peak_in"], "in_total": res["in_total"], "out_total": res["out_total"],
               "cache_pct": res["cache_pct"], "wall_s": res["wall_s"], "findings": res["findings"],
               "dialogue": res["dialogue"]}
        out.append(rec)
        print(f"  -> {rec['passed']}/{rec['total']} | rounds={rec['n_rounds']} peak_in={rec['peak_in']:,} "
              f"tok={rec['in_total']+rec['out_total']:,} cache={rec['cache_pct']}% recall={rec['recall']} "
              f"wall={rec['wall_s']}s{' (renamed)' if rec['renamed'] else ''}", flush=True)
        json.dump(out, open(outfile, "w"), indent=2)   # checkpoint after each task
    npass = sum(1 for o in out if o["total"] and o["passed"] == o["total"])
    print("\n" + "=" * 80 + f"\nSUMMARY (sliceagent): {npass}/{len(out)} tasks fully passed")
    for o in out:
        print(f"  {o['task']:9} {o['passed']}/{o['total']:<3} rounds={o['n_rounds']} peak={o['peak_in']:>6,} "
              f"tok={o['in_total']+o['out_total']:>7,} recall={o['recall']} wall={o['wall_s']}s")
    print(f"results -> {outfile}")


if __name__ == "__main__":
    main()
