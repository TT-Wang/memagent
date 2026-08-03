"""HEAD seed-decomposition baseline — P0-T execution step 1.

The Task #123 seed table was measured at `40888ee`, which is the pin review #122 found DEFECTIVE:
the protected_deps producer was deleted there, so OPEN FILES carried NO dependency closure while
the shares were measured (its 39–63% is a floor, not a ceiling), and `turn_contract` still rendered
its 243-byte mechanical boilerplate (6.6% of that seed) which `8fa281a` since suppressed. Every
share in that table therefore needs recomputing before any ordering work is ranked against it.

No new instrumentation: the P0.2 admission journal already records one row per ADMITTED context
block with its exact byte count. This driver runs real turns against a scratch repo, then reads
those rows back into a per-region decomposition.

  .venv/bin/python -m evals.seed_baseline --turns 6 --model deepseek-v4-flash
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ("src", os.path.join("packages", "sliceagent-core", "src"),
           os.path.join("packages", "sliceagent-cli", "src")):
    sys.path.insert(0, os.path.join(_REPO, _p))

# An edit-heavy profile (the s2/s3 shape, where seed rebuild dominates) plus one read-only turn that
# serves as the NO-EDIT positive control: if a turn changes no file, its prefix should survive.
TURNS = [
    "Read calc.py and tell me what divide() does. Do not edit anything.",
    "Add a guard so divide raises ValueError('division by zero') when b == 0.",
    "Add a subtract(a, b) function to calc.py.",
    "Add a docstring to every function in calc.py.",
    "Which functions does calc.py export now? Answer from what you know; do not re-read.",
    "Add a multiply(a, b) function with a docstring.",
]
_FILES = {
    "calc.py": "def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n    return a / b\n",
    "util.py": "def clamp(v, lo, hi):\n    return max(lo, min(hi, v))\n",
    "README.md": "# calc\n\nA tiny calculator used by the seed-baseline profile.\n",
}


def _workspace(repo: str = "") -> str:
    if repo:
        dest = tempfile.mkdtemp(prefix="seed-baseline-repo-")
        root = os.path.join(dest, os.path.basename(repo.rstrip("/")) or "repo")
        shutil.copytree(repo, root, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__",
                                                      ".venv", "dist", "build", ".cache"))
        return root
    root = tempfile.mkdtemp(prefix="seed-baseline-")
    for name, body in _FILES.items():
        with open(os.path.join(root, name), "w", encoding="utf-8") as f:
            f.write(body)
    return root


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default="", help="real repo to copy as the workspace "
                    "(default: a synthetic 3-file fixture, which CANNOT represent open_files share)")
    ap.add_argument("--model", default=os.environ.get("AGENT_MODEL", "deepseek-v4-flash"))
    ap.add_argument("--turns", type=int, default=len(TURNS))
    ap.add_argument("--max-steps", type=int, default=14)
    ap.add_argument("--out", default=os.path.join(_REPO, "evals", "seed_baseline_head.json"))
    a = ap.parse_args()

    from sliceagent_cli.code_grep import make_grep_tool
    from sliceagent_cli.code_index import make_code_index
    from sliceagent_cli.coding_tool_host import LocalToolHost
    from sliceagent_core.events import make_dispatcher
    from sliceagent_core.llm import OpenAILLM
    from sliceagent_core.loop import run_turn
    from sliceagent_core.pfc import Slice, record_user, slice_sink
    from sliceagent_core.intent import TurnAdmission
    from sliceagent_core.records import AdmissionMetrics, Journal
    from sliceagent_core.seed import make_build_slice

    workspace = _workspace(a.repo)
    session = f"seed-baseline-{int(time.time())}"
    journal = Journal(session, root=tempfile.mkdtemp(prefix="seed-baseline-records-"))
    state = Slice(); state.reset(TURNS[0])
    tools = LocalToolHost(workspace)
    tools.registry.register(make_grep_tool(tools))
    retriever = make_code_index(workspace)
    llm = OpenAILLM(model=a.model)

    plan_box: dict = {"plan": None}
    admission = AdmissionMetrics(journal, lambda: plan_box["plan"], lambda: state)
    # Edit detection must count MUTATING TOOL CALLS: `set(edited_files)` is unchanged when the same
    # file is edited again, so a set diff reported 1/6 edit turns for a 4-edit profile.
    from sliceagent_core.events import ToolResult
    _edits = {"n": 0}
    _MUTATORS = {"edit_file", "str_replace", "append_to_file", "write_file", "create_file"}

    def _count_edits(e):
        if isinstance(e, ToolResult) and e.name in _MUTATORS and not e.failing:
            _edits["n"] += 1

    dispatch = make_dispatcher(slice_sink(state), admission, _count_edits)

    edits_per_turn: list[bool] = []
    for i, prompt in enumerate(TURNS[:a.turns], 1):
        # PRODUCTION SHAPE: the interactive CLI builds a mechanical TurnAdmission (cli.py:1360).
        # Calling record_user with no contract instead fires the analyze_turn FALLBACK, which
        # renders a turn_contract block that production never shows — the first run of this driver
        # measured 20.7% turn_contract share purely as that harness artifact.
        record_user(state, prompt, contract=TurnAdmission(request_text=prompt))
        inner = make_build_slice(state, tools, retriever, None, prompt, session_id=session,
                                 model_id=a.model)

        def _build(_inner=inner):
            plan = _inner()
            plan_box["plan"] = plan
            return plan

        print(f"[{i}/{min(a.turns, len(TURNS))}] {prompt[:60]}", flush=True)
        try:
            run_turn(build_slice=_build, llm=llm, tools=tools, dispatch=dispatch,
                     max_steps=a.max_steps)
        except Exception as exc:  # noqa: BLE001 — one bad turn must not lose the earlier rows
            import traceback
            print(f"  turn failed: {type(exc).__name__}: {str(exc)[:140]}", file=sys.stderr)
            traceback.print_exc(limit=3, file=sys.stderr)
        edits_per_turn.append(_edits["n"] > 0)
        _edits["n"] = 0
        state.seal()

    rows = journal.read("admission")
    turn_rows = journal.read("turn_regions")
    by_region: dict[str, list[int]] = {}
    for r in rows:
        by_region.setdefault(str(r.get("block") or "?"), []).append(int(r.get("chars") or 0))
    total = sum(sum(v) for v in by_region.values()) or 1
    table = sorted(((name, sum(v), len(v), sum(v) / total * 100)
                    for name, v in by_region.items()), key=lambda t: -t[1])

    print(f"\n== HEAD seed decomposition ({len(turn_rows)} turns, {len(rows)} admitted blocks) ==")
    print(f"{'block':30s} {'chars':>9s} {'n':>4s} {'share':>7s}")
    for name, chars, n, share in table:
        print(f"{name:30s} {chars:9,d} {n:4d} {share:6.1f}%")
    print(f"\nedit turns: {sum(edits_per_turn)}/{len(edits_per_turn)} "
          f"(pattern {''.join('E' if e else '.' for e in edits_per_turn)}) "
          "— '.' turns are the no-edit positive control for T2a")

    payload = {
        "pin": "HEAD", "model": a.model, "turns": len(turn_rows),
        "edit_pattern": ["edit" if e else "no-edit" for e in edits_per_turn],
        "regions": [{"block": n, "chars": c, "admissions": k, "share_pct": round(s, 2)}
                    for n, c, k, s in table],
        "turn_regions": turn_rows,
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\nwrote {a.out}")
    shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
