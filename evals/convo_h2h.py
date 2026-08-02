"""Conversational head-to-head: sliceagent vs Kimi Code (SAME model, Moonshot kimi-k2.7-code) on
INTENT RECOGNITION + CONVERSATION SMOOTHNESS — NOT problem-solving.

Single-turn conversational prompts over a tiny fixture repo: a greeting, a pure-knowledge question,
two code-explanation questions (answer + maybe read, but must NOT edit), an ambiguous remark, and a
naming-advice question. For each agent we capture the final answer text + which tools it called, so we
can measure the intent signal — did it EDIT a question, or over-tool a greeting? — and (separately)
judge smoothness from the captured answers.

Run (Moonshot, CN-direct, NO proxy):
  cd ~/Desktop/sliceagent
  set -a; . "../agent design/.env"; set +a
  export LLM_API_KEY="$MOONSHOT_API_KEY" LLM_BASE_URL="https://api.moonshot.cn/v1" AGENT_MODEL=kimi-k2.7-code
  PYTHONPATH=src .venv/bin/python -m evals.convo_h2h
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

KIMI_BIN = os.path.expanduser("~/.kimi-code/bin/kimi")
EDIT_TOOLS = {"edit_file", "str_replace", "append_to_file", "write", "edit", "create",
              "write_file", "str_replace_editor", "apply_patch", "create_file",
              "multiedit", "notebookedit"}   # matched case-INSENSITIVELY (Kimi emits 'Edit'/'Write')

def _is_edit(name: str) -> bool:
    return (name or "").lower() in EDIT_TOOLS

# ----------------------------------------------------------------------------- fixture
FIXTURE = {
    "README.md": "# miniconf\nA tiny config-parsing + retry utility package.\n",
    "miniconf/__init__.py": "from .parser import parse_config\nfrom .retry import retry\n",
    "miniconf/parser.py": (
        "def parse_config(text):\n"
        '    """Parse \'key = value\' lines into a dict. Blank lines and \'#\' comments are skipped."""\n'
        "    result = {}\n"
        "    for line in text.splitlines():\n"
        "        line = line.strip()\n"
        "        if not line or line.startswith('#'):\n"
        "            continue\n"
        "        key, value = line.split('=', 1)   # a line with no '=' raises ValueError\n"
        "        result[key.strip()] = value.strip()\n"
        "    return result\n"
    ),
    "miniconf/retry.py": (
        "import time\n\n"
        "def retry(times=3, delay=0.5):\n"
        '    """Re-call the wrapped function up to `times` times on any exception, sleeping `delay`\n'
        '    seconds between attempts; re-raises the last exception if all attempts fail."""\n'
        "    def deco(fn):\n"
        "        def wrapper(*a, **k):\n"
        "            last = None\n"
        "            for _ in range(times):\n"
        "                try:\n"
        "                    return fn(*a, **k)\n"
        "                except Exception as e:\n"
        "                    last = e\n"
        "                    time.sleep(delay)\n"
        "            raise last\n"
        "        return wrapper\n"
        "    return deco\n"
    ),
}

# id, prompt, kind, expectation (the intent the agent should recognize)
PROMPTS = [
    ("greet",   "hey there", "chat", "no_tools"),
    ("know",    "in plain terms, what's the difference between a process and a thread?", "knowledge", "no_tools"),
    ("safety",  "how does config parsing work in this project, and does it handle malformed input safely?", "code_q", "read_no_edit"),
    ("explain", "what does the retry helper do?", "code_q", "read_no_edit"),
    ("ambig",   "the config loading feels off to me", "ambiguous", "clarify_or_investigate_no_blind_edit"),
    ("naming",  "is `parse_config` a clear name, or would something else read better?", "advice", "no_edit"),
]


def _make_workdir() -> str:
    wd = tempfile.mkdtemp(prefix="convo-")
    for rel, content in FIXTURE.items():
        p = os.path.join(wd, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(rel) else None
        open(p, "w").write(content)
    return wd


# ----------------------------------------------------------------------------- sliceagent
def run_sliceagent(prompt: str, workdir: str, model: str) -> dict:
    from sliceagent.pfc import Slice, slice_sink, record_user
    from sliceagent.seed import make_build_slice
    from sliceagent.loop import run_turn
    from sliceagent.tools import LocalToolHost
    from sliceagent.code_index import make_code_index
    from sliceagent.memory import NullMemory
    from sliceagent.events import make_dispatcher, ToolResult, AssistantText
    from sliceagent.llm import OpenAILLM

    state = Slice(); state.reset(prompt)
    tools = LocalToolHost(root=workdir)
    retriever = make_code_index(workdir)
    texts: list[str] = []
    toolnames: list[str] = []

    def collect(e):
        if isinstance(e, AssistantText):
            if (e.content or "").strip():       # AssistantText field is .content (not .text)
                texts.append(e.content.strip())
        elif isinstance(e, ToolResult):
            toolnames.append(getattr(e, "name", "") or "")

    dispatch = make_dispatcher(slice_sink(state), collect)
    llm = OpenAILLM(model=model, timeout=90.0)
    record_user(state, prompt)
    build = make_build_slice(state, tools, retriever, NullMemory(), prompt)
    t0 = time.time()
    res = run_turn(build_slice=build, llm=llm, tools=tools, dispatch=dispatch, max_steps=12)
    return {"agent": "sliceagent", "text": (texts[-1] if texts else ""), "all_text": texts,
            "tools": toolnames, "steps": res.steps, "stop": res.stop_reason,
            "wall": round(time.time() - t0, 1)}


# ----------------------------------------------------------------------------- kimi
def run_kimi(prompt: str, workdir: str) -> dict:
    cmd = [KIMI_BIN, "-p", prompt, "--output-format", "stream-json"]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"agent": "kimi", "text": "", "all_text": [], "tools": [], "steps": 0,
                "stop": "timeout", "wall": 300.0}
    texts: list[str] = []
    toolnames: list[str] = []
    for ln in proc.stdout.splitlines():
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("role") == "assistant":
            c = o.get("content")
            if isinstance(c, str) and c.strip():
                texts.append(c.strip())
            for tc in (o.get("tool_calls") or []):
                fn = (tc.get("function") or {}).get("name") or tc.get("name") or "?"
                toolnames.append(fn)
    return {"agent": "kimi", "text": (texts[-1] if texts else ""), "all_text": texts,
            "tools": toolnames, "steps": len(toolnames), "stop": f"rc={proc.returncode}",
            "wall": round(time.time() - t0, 1)}


def _intent_flags(kind: str, expect: str, tools: list[str]) -> dict:
    edited = any(_is_edit(t) for t in tools)
    n = len(tools)
    return {
        "edited": edited,
        "n_tools": n,
        # the failure conditions, per expectation:
        "fail_edited_a_question": edited,                       # NOTHING here asks for a change → any edit is wrong
        "fail_overtooled_chat": (expect == "no_tools" and n > 0),
    }


def main() -> None:
    model = os.environ.get("AGENT_MODEL", "kimi-k2.7-code")
    only = sys.argv[1] if len(sys.argv) > 1 else None      # optional: a single prompt id
    results = []
    for pid, prompt, kind, expect in PROMPTS:
        if only and pid != only:
            continue
        for runner in ("sliceagent", "kimi"):
            wd = _make_workdir()                              # FRESH identical fixture per run
            print(f"  · {pid:8} [{runner}] …", flush=True)
            try:
                r = run_sliceagent(prompt, wd, model) if runner == "sliceagent" else run_kimi(prompt, wd)
            except Exception as e:  # noqa: BLE001
                r = {"agent": runner, "text": "", "all_text": [], "tools": [], "steps": 0,
                     "stop": f"ERR {type(e).__name__}: {e}", "wall": 0.0}
            shutil.rmtree(wd, ignore_errors=True)
            r.update({"id": pid, "prompt": prompt, "kind": kind, "expect": expect})
            r["intent"] = _intent_flags(kind, expect, r["tools"])
            results.append(r)
            fl = r["intent"]
            print(f"      tools={r['tools']} edited={fl['edited']} steps={r['steps']} {r['wall']}s")

    out = os.path.join(os.path.dirname(__file__), "convo_results.json")
    json.dump(results, open(out, "w"), indent=2)
    print(f"\nsaved {out} ({len(results)} runs)")


if __name__ == "__main__":
    main()
