"""LIVE validation: does the PAGED-OUT HISTORY manifest make the model CALL recall_history?

The active-ask channel was measured at recall=0. The thesis: it was dead because the cache was
INVISIBLE, not because active recall is useless. This driver runs the multi-session scenario the
single-loop h2h benchmarks CANNOT exercise (they have nothing paged out):

  Topic A, turn 1 : "find every caller of Config.load" — the model locates them; the turn is cached.
  Topic B, turn 2 : "rename Config.load in the caller files you found earlier" — the caller list is
                    NO LONGER in the slice (fresh topic → findings/recent reset); it lives only in the
                    episodic cache, surfaced on the PAGED-OUT HISTORY manifest with its fetch call.

Pass = the model calls recall_history(turns=[1]) to recover the list instead of re-greping blind.
CONTROL = same run with the manifest disabled (session_id="" → region suppressed; the recall_history
tool is STILL registered) — isolates the manifest as the cause. Faithful durable path (MememMemory +
episode sink + history tool), mirroring cli.py wiring, scripted headless.

Run: LLM_API_KEY=… LLM_BASE_URL=… AGENT_MODEL=kimi-k2.7-code \
     PYTHONPATH=src .venv/bin/python -m evals.validate_recall --runs 3
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _make_workspace() -> str:
    wd = tempfile.mkdtemp(prefix="recall-val-")
    pkg = os.path.join(wd, "pkg")
    os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    open(os.path.join(pkg, "config.py"), "w").write(
        "class Config:\n"
        "    @staticmethod\n"
        "    def load(path):\n"
        "        \"\"\"Load config from a path.\"\"\"\n"
        "        return {'path': path}\n")
    # three genuine callers + one distractor that does NOT call it
    open(os.path.join(pkg, "api.py"), "w").write(
        "from .config import Config\n\ndef boot():\n    return Config.load('api.toml')\n")
    open(os.path.join(pkg, "cli.py"), "w").write(
        "from .config import Config\n\ndef main():\n    cfg = Config.load('cli.toml')\n    return cfg\n")
    open(os.path.join(pkg, "service.py"), "w").write(
        "from .config import Config\n\nclass Service:\n    def start(self):\n        return Config.load('svc.toml')\n")
    open(os.path.join(pkg, "util.py"), "w").write(
        "def helper(x):\n    return x + 1\n")  # distractor: no Config.load
    try:
        subprocess.run(["git", "init", "-q"], cwd=wd, check=False)
        subprocess.run(["git", "add", "-A"], cwd=wd, check=False)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                       cwd=wd, check=False)
    except Exception:
        pass
    return wd


CALLERS = ("api.py", "cli.py", "service.py")
T1 = "Find every file in the pkg package that calls Config.load and list those file paths. Do not edit anything."
T2 = ("Now rename the method Config.load to Config.from_file. Apply it in config.py AND in exactly the "
      "caller files you located earlier in this session — you already found them, so don't re-scan the "
      "whole repo, reuse what you found.")

# ── NON-RE-DERIVABLE scenario: a user-stated fact that lives ONLY in the cache (no file to re-grep) ──
# This is where active recall actually earns its call: the values are in conversation/cache, not code,
# so the model CANNOT re-derive them — it must page turn 1 back in (or guess wrong). ON should recall +
# get the exact values; OFF (no manifest cue) should fail to recover them.
CODES = {"AUTH_FAIL": 4101, "RATE_LIMIT": 4290, "BAD_INPUT": 4233, "TIMEOUT": 4504}
C_T1 = ("We've finalized four API error codes for this project: AUTH_FAIL=4101, RATE_LIMIT=4290, "
        "BAD_INPUT=4233, TIMEOUT=4504. Please acknowledge by listing all four back to me so I know "
        "they're recorded. Do NOT create any files yet.")
C_T2 = ("Create pkg/errors.py defining the four API error-code constants we finalized earlier in this "
        "session, each set to its exact integer value. Use the values we agreed on — don't invent new ones.")


def _run_once(manifest_on: bool, model: str, scenario: str = "rename") -> dict:
    # fresh, isolated durable vault per run (MememMemory reads SLICEAGENT_VAULT at construction)
    vault = tempfile.mkdtemp(prefix="recall-vault-")
    os.environ["SLICEAGENT_VAULT"] = vault
    wd = _make_workspace()
    try:
        from sliceagent.memory import MememMemory
        from sliceagent.session import Session, make_topic_tools
        from sliceagent.tools import LocalToolHost
        from sliceagent.code_index import make_code_index
        from sliceagent.hippocampus import make_episode_sink
        from sliceagent.events import ToolResult, SliceBuilt, make_dispatcher
        from sliceagent.hooks import CatastrophicSafeguardHook, CompositeHooks
        from sliceagent.hippocampus import make_search_history_tool
        from sliceagent.llm import OpenAILLM
        from sliceagent.loop import run_turn
        from sliceagent.pfc import record_user, slice_sink
        from sliceagent.seed import make_build_slice
        from sliceagent.text_utils import one_line

        memory = MememMemory()
        session = Session(memory)
        sid = session.session_id
        tools = LocalToolHost(root=wd)
        for t in make_topic_tools(session):
            tools.registry.register(t)
        tools.registry.register(make_search_history_tool(memory, sid))   # recall_history ALWAYS registered
        retriever = make_code_index(wd)
        episodic = make_episode_sink(memory, session_id=sid,
                                     task_id_fn=lambda: session.active_id or "t-none",
                                     title_fn=lambda: one_line(session.active().goal, 80) if session.active_id else "")

        recalls: list[dict] = []
        slices: dict[int, str] = {}
        cur_turn = {"n": 0}

        def _instr(e):
            if isinstance(e, ToolResult) and getattr(e, "name", None) == "recall_history":
                recalls.append({"turn": cur_turn["n"], "args": e.args})
            if isinstance(e, SliceBuilt) and cur_turn["n"] not in slices:
                slices[cur_turn["n"]] = e.rendered     # first slice rendered this turn

        sinks = [slice_sink(session), _instr]
        if episodic is not None:
            sinks.append(episodic)
        dispatch = make_dispatcher(*sinks)
        hooks = CompositeHooks(CatastrophicSafeguardHook())
        llm = OpenAILLM(model=model, timeout=90.0)

        sid_arg = sid if manifest_on else ""   # OFF control: no manifest, tool still present
        t1, t2 = (C_T1, C_T2) if scenario == "constants" else (T1, T2)

        # ---- Topic A / turn 1: establish the fact (cached on TurnEnd) ----
        cur_turn["n"] = 1
        session.new_topic(t1)
        record_user(session.active(), t1)
        b1 = make_build_slice(session, tools, retriever, memory, t1, sid_arg)
        run_turn(build_slice=b1, llm=llm, tools=tools, dispatch=dispatch, hooks=hooks, max_steps=14)

        # ---- Topic B / turn 2: dependent task; the turn-1 fact is now paged out ----
        cur_turn["n"] = 2
        session.new_topic(t2)
        record_user(session.active(), t2)
        b2 = make_build_slice(session, tools, retriever, memory, t2, sid_arg)
        t2_slice = b2()[1]["content"]            # capture the T2 starting slice (the manifest, if on)
        run_turn(build_slice=b2, llm=llm, tools=tools, dispatch=dispatch, hooks=hooks, max_steps=16)

        # ---- measure ----
        t2_recalls = [r for r in recalls if r["turn"] == 2]
        common = {
            "scenario": scenario,
            "manifest_on": manifest_on,
            "manifest_in_t2_slice": "# PAGED-OUT HISTORY" in t2_slice,
            "recall_calls_t2": len(t2_recalls),
            "recall_args_t2": [r["args"] for r in t2_recalls],
            "manifest_excerpt": _excerpt(t2_slice),
        }
        if scenario == "constants":
            # the FACT is non-re-derivable (no file has it) → accuracy is the real signal
            ep = os.path.join(wd, "pkg", "errors.py")
            src = open(ep).read() if os.path.exists(ep) else ""
            present = {name: (str(val) in src) for name, val in CODES.items()}
            common.update({
                "file_created": bool(src),
                "codes_present": present,
                "codes_present_n": sum(1 for v in present.values() if v),  # exact values recovered (0..4)
            })
            return common
        cfg = open(os.path.join(wd, "pkg", "config.py")).read()
        hit = {}
        for f in CALLERS:
            s = open(os.path.join(wd, "pkg", f)).read()
            hit[f] = ("from_file" in s and "Config.load" not in s)
        common.update({
            "renamed_def": "def from_file" in cfg and "def load" not in cfg,
            "callers_hit": hit,
            "callers_hit_n": sum(1 for v in hit.values() if v),
            "distractor_untouched": "from_file" not in open(os.path.join(wd, "pkg", "util.py")).read(),
        })
        return common
    finally:
        shutil.rmtree(wd, ignore_errors=True)
        shutil.rmtree(vault, ignore_errors=True)


def _excerpt(slice_text: str) -> str:
    if "# PAGED-OUT HISTORY" not in slice_text:
        return "(no manifest in slice)"
    i = slice_text.index("# PAGED-OUT HISTORY")
    return slice_text[i:i + 600]


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default=os.environ.get("AGENT_MODEL", "kimi-k2.7-code"))
    ap.add_argument("--conditions", default="on,off")
    ap.add_argument("--scenario", default="constants", choices=["rename", "constants"],
                    help="constants = NON-re-derivable (recall necessary); rename = re-derivable (control)")
    ap.add_argument("--out", default=os.path.join(ROOT, "evals", "h2h", "recall_validation.json"))
    args = ap.parse_args()
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from sliceagent.cli import _load_env
    _load_env()

    scn = args.scenario
    conds = [c == "on" for c in args.conditions.split(",")]
    results = []
    for on in conds:
        label = "MANIFEST-ON" if on else "MANIFEST-OFF (control)"
        print(f"\n=== {label} · scenario={scn} · {args.runs} runs · model={args.model} ===")
        for i in range(args.runs):
            try:
                r = _run_once(on, args.model, scn)
            except Exception as e:  # noqa: BLE001
                r = {"manifest_on": on, "scenario": scn, "error": f"{type(e).__name__}: {e}"}
            results.append(r)
            if "error" in r:
                print(f"  run{i}: ERROR {r['error']}")
            elif scn == "constants":
                print(f"  run{i}: recall_calls_t2={r['recall_calls_t2']} args={r['recall_args_t2']} "
                      f"manifest_in_slice={r['manifest_in_t2_slice']} "
                      f"EXACT_VALUES_RECOVERED={r['codes_present_n']}/4 file_created={r['file_created']}")
            else:
                print(f"  run{i}: recall_calls_t2={r['recall_calls_t2']} args={r['recall_args_t2']} "
                      f"manifest_in_slice={r['manifest_in_t2_slice']} callers_hit={r['callers_hit_n']}/3")

    def _summ(on):
        rs = [r for r in results if r.get("manifest_on") == on and "error" not in r]
        if not rs:
            return "n/a"
        called = sum(1 for r in rs if r["recall_calls_t2"] > 0)
        if scn == "constants":
            acc = sum(r["codes_present_n"] for r in rs) / (4 * len(rs))
            full = sum(1 for r in rs if r["codes_present_n"] == 4)
            return f"recall-call {called}/{len(rs)}  ·  exact-value accuracy {acc:.0%}  ·  all-4-correct {full}/{len(rs)}"
        cov = sum(r["callers_hit_n"] for r in rs) / (3 * len(rs))
        return f"recall-call {called}/{len(rs)}  ·  caller-coverage {cov:.0%}"
    print("\n================ SUMMARY ================")
    print(f"  MANIFEST-ON : {_summ(True)}")
    print(f"  MANIFEST-OFF: {_summ(False)}")
    json.dump(results, open(args.out, "w"), indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
