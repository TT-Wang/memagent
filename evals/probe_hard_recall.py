"""HARD cross-slice recall scenarios (watch-item #5), UNCOMMITTED. Stresses the channel under:
  H1 multi-topic interleave — facts established in two EARLIER topics (each topic switch wipes the slice
     via reset(); the episode cache persists by session_id) must both be recalled after the switches.
  H2 very-detailed recall — a precise value buried in the MIDDLE of a many-key file, recalled exactly.
  H3 deep-session — a fact from turn 1 pushed BEYOND the 8-turn manifest window, reachable only by
     recall_history(search=...) / the index.
Sources are deleted after their topic, so re-reading is impossible — recall is the only path.

Run (per model): PYTHONPATH=src AGENT_MODEL=... LLM_API_KEY=... LLM_BASE_URL=... .venv/bin/python evals/probe_hard_recall.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _mk(wd, rel, content):
    p = os.path.join(wd, rel)
    os.makedirs(os.path.dirname(p) or wd, exist_ok=True)
    open(p, "w").write(content)


def run_topics(steps, *, session_id, workdir, model, memory, max_steps=12):
    """Drive a multi-topic session. steps = [{prompt, new_topic?, clean?}]. new_topic=True wipes the slice
    (reset) to simulate a topic switch; the episode cache (session_id) persists, so recall must bridge."""
    from sliceagent.pfc import Slice, consolidate_checkpoint, record_user, slice_sink
    from sliceagent.seed import make_build_slice
    from sliceagent.text_utils import one_line
    from sliceagent.loop import run_turn
    from sliceagent.tools import LocalToolHost
    from sliceagent.code_index import make_code_index
    from sliceagent.events import AssistantText, ToolResult, make_dispatcher
    from sliceagent.hippocampus import make_episode_sink
    from sliceagent.code_grep import make_grep_tool
    from sliceagent.hippocampus import make_search_history_tool
    from sliceagent.llm import OpenAILLM

    state = Slice(); state.reset(steps[0]["prompt"])
    base = LocalToolHost(workdir); base.registry.register(make_grep_tool(base))
    if getattr(memory, "is_durable", False):
        base.registry.register(make_search_history_tool(memory, session_id))
    retriever = make_code_index(workdir)
    episodic = make_episode_sink(memory, session_id=session_id, task_id_fn=lambda: "t",
                                 title_fn=lambda: one_line(state.goal, 80))
    reply, tools = [""], []
    def cap(e):
        if isinstance(e, AssistantText) and (e.content or "").strip():
            reply[0] = e.content
        elif isinstance(e, ToolResult):
            tools.append(e.name)
    dispatch = make_dispatcher(slice_sink(state), episodic, cap)
    llm = OpenAILLM(model=model, timeout=120.0); llm.set_cache_key(session_id)
    build = make_build_slice(state, base, retriever, memory, steps[0]["prompt"], session_id=session_id)

    out = []
    for i, st in enumerate(steps):
        if i > 0:
            if st.get("new_topic"):
                state.reset(st["prompt"])           # TOPIC SWITCH — wipe the slice (findings/world gone)
            else:
                state.goal = st["prompt"]
        reply[0] = ""; tools.clear()
        record_user(state, st["prompt"])
        try:
            run_turn(build_slice=build, llm=llm, tools=base, dispatch=dispatch, max_steps=max_steps,
                     consolidate=lambda: consolidate_checkpoint(state, compact=False))
        except Exception as e:  # noqa: BLE001
            reply[0] = f"(err {type(e).__name__}: {e})"
        out.append({"reply": reply[0], "tools": list(tools)})
        for rel in st.get("clean", []):
            p = os.path.join(workdir, rel)
            if os.path.exists(p):
                os.remove(p)
    return out


def h1(model, memory):
    wd = tempfile.mkdtemp(prefix="h1-")
    _mk(wd, "billing.md", "service: billing\nAPI_BASE=api-ALPHA-X7\nregion: us-west\nplan: pro\n")
    _mk(wd, "auth.md", "service: auth\nSESSION_TIMEOUT_S=900-BRAVO\nmode: jwt\nrotate: daily\n")
    steps = [
        {"prompt": "Topic: billing. Use read_file to read billing.md and tell me the API_BASE value.", "clean": ["billing.md"]},
        {"prompt": "New topic: auth. Use read_file to read auth.md and tell me the SESSION_TIMEOUT_S value.", "new_topic": True, "clean": ["auth.md"]},
        {"prompt": "New topic: housekeeping. Create scratch.txt containing exactly: ok", "new_topic": True},
        {"prompt": "Going back to the earlier topics — what was the billing API_BASE, and what was the auth SESSION_TIMEOUT_S? Give both exact values."},
    ]
    out = run_topics(steps, session_id="h1-multitopic", workdir=wd, model=model, memory=memory)
    r = out[-1]["reply"]
    return {"name": "H1 multi-topic interleave (2 facts across 2 reset topics)",
            "billing_recalled": "api-ALPHA-X7" in r, "auth_recalled": "900-BRAVO" in r,
            "recalled_tool": any("recall_history" in t for t in out[-1]["tools"]),
            "PASS": "api-ALPHA-X7" in r and "900-BRAVO" in r, "reply": r[:220]}


def h2(model, memory):
    wd = tempfile.mkdtemp(prefix="h2-")
    lines = [f"PARAM_{i:02d}=val_{i:02d}" for i in range(30)]
    lines[15] = "RETRY_BACKOFF_MS=4250-CHARLIE"
    _mk(wd, "settings.ini", "\n".join(lines) + "\n")
    steps = [
        {"prompt": "Use read_file to read settings.ini and tell me ONLY how many PARAM_ lines it has. Do not list values.", "clean": ["settings.ini"]},
        {"prompt": "New topic: create a.txt with the word alpha.", "new_topic": True},
        {"prompt": "New topic: what is 8 times 9? Just the number.", "new_topic": True},
        {"prompt": "Earlier you read settings.ini. What was the EXACT value of RETRY_BACKOFF_MS?"},
    ]
    out = run_topics(steps, session_id="h2-detail", workdir=wd, model=model, memory=memory)
    r = out[-1]["reply"]
    return {"name": "H2 very-detailed recall (precise mid-file value)",
            "recalled_tool": any("recall_history" in t for t in out[-1]["tools"]),
            "PASS": "4250-CHARLIE" in r, "reply": r[:220]}


def h3(model, memory):
    wd = tempfile.mkdtemp(prefix="h3-")
    _mk(wd, "seed.md", "project notes\nmarker_field: DEEPVAL=ECHO-9931\nstatus: draft\n")
    fillers = [{"prompt": f"Create note{i}.txt containing exactly: n{i}", "new_topic": (i % 2 == 0)} for i in range(9)]
    steps = ([{"prompt": "Use read_file to read seed.md and tell me ONLY how many lines it has. Do not list values.", "clean": ["seed.md"]}]
             + fillers
             + [{"prompt": "Way back at the very start of this session you read a file with a DEEPVAL. What was that DEEPVAL value, exactly?"}])
    out = run_topics(steps, session_id="h3-deep", workdir=wd, model=model, memory=memory, max_steps=10)
    r = out[-1]["reply"]
    return {"name": "H3 deep-session (turn 1 beyond the 8-turn manifest → search recall)",
            "recalled_tool": any("recall_history" in t for t in out[-1]["tools"]),
            "PASS": "ECHO-9931" in r, "reply": r[:220]}


def main():
    from sliceagent.memory import make_memory
    model = os.environ.get("AGENT_MODEL", "deepseek-chat")
    memory = make_memory()
    print(f"# HARD recall scenarios · model={model} · memory={type(memory).__name__}\n", flush=True)
    rows = []
    for fn in (h1, h2, h3):
        t0 = time.time()
        try:
            r = fn(model, memory)
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc(); r = {"name": fn.__name__, "PASS": False, "error": str(e)}
        r["wall"] = round(time.time() - t0, 1)
        rows.append(r)
        print(f"[{'PASS' if r.get('PASS') else 'FAIL'}] {r.get('name'):52} ({r['wall']}s)", flush=True)
        for k in ("billing_recalled", "auth_recalled", "recalled_tool", "error"):
            if k in r:
                print(f"      {k}: {r[k]}", flush=True)
        print(f"      reply: {r.get('reply', '')}", flush=True)
    n = sum(1 for r in rows if r.get("PASS"))
    print(f"\n==================== {model}: {n}/{len(rows)} hard scenarios passed ====================", flush=True)
    sys.exit(0 if n == len(rows) else 1)


if __name__ == "__main__":
    main()
