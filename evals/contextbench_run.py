"""ContextBench runner — sliceagent's context retrieval measured against human gold context.

ContextBench (arXiv:2602.05892) evaluates RECORDED trajectories: for each issue-resolution task it
knows the human-annotated gold context (the file spans a correct fix actually needs), and scores an
agent's retrieval recall / precision / efficiency along its trajectory. We feed it sliceagent
trajectories in the canonical `traj_data` shape its parser consumes:
    {"instance_id": ..., "traj_data": {"pred_steps": [{"files": [...], "spans": {...}}, ...],
                                       "pred_files": [...], "pred_spans": {...}}}

TWO LEDGERS, reported separately — this is the methodological crux, not a detail:
  * pulled  — only what the model explicitly FETCHED (read_file / grep hits). This is the shape the
              peer agents in the leaderboard have, since a transcript agent has no other way in.
  * resident — pulled PLUS what the bounded slice PUSHED into context without a tool call (the
              OPEN FILES working set and its dependency closure). This is what the model actually
              saw, and it is the whole point of a reconstructed-slice architecture.
Reporting only `pulled` would understate recall; reporting only `resident` would flatter efficiency.
Both go in the results file; the deck reports both.

Usage (network + model required; the repo clone is read-only, cached under --work):
  .venv/bin/python -m evals.contextbench_run --data data/contextbench_verified.parquet \
      --n 50 --model deepseek-v4-flash --out evals/contextbench/run1
Then score with the official harness (separate checkout):
  python -m contextbench.evaluate --gold <verified.parquet> --pred <out>/pred_resident.jsonl \
      --out <out>/metrics_resident.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ("src", os.path.join("packages", "sliceagent-core", "src"),
           os.path.join("packages", "sliceagent-cli", "src")):
    sys.path.insert(0, os.path.join(_REPO, _p))

# The slice renders working-set files as "### <path> (N lines — full)" or "### <path> (lines A-B)".
_SLICE_FILE = re.compile(r"^### (\S+) \((?:(\d+) lines — full|lines (\d+)-(\d+))", re.M)
# CONTEXT-BEARING reads only. list_files/glob return NAMES, not content — counting a directory
# listing as retrieved context is what the smoke run exposed (4 "files" that were all directories,
# zero real spans). grep returns matching LINES: real content, credited at the hit lines.
_READ_TOOLS = {"read_file", "grep"}
# read_file renders numbered lines; grep renders "path:line:text" — both give exact line numbers.
_NUMBERED = re.compile(r"^\s*(\d+)[\t|:]", re.M)
_GREP_HIT = re.compile(r"^([^\s:]+):(\d+):", re.M)


def _load_tasks(path: str, n: int, lang: str) -> list[dict]:
    """Tasks from the ContextBench dataset. JSONL is the native input (zero extra deps — convert
    the published parquet once with pyarrow OUTSIDE this venv); .parquet is read only if pyarrow
    happens to be importable."""
    if path.endswith(".parquet"):
        import pyarrow.parquet as pq   # optional: not a runtime dependency of sliceagent
        rows = pq.read_table(path).to_pylist()
    else:
        rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    if lang:
        rows = [r for r in rows if str(r.get("language") or "") == lang]
    rows.sort(key=lambda r: str(r.get("instance_id")))   # deterministic slice, no sampling seed
    return rows[:n]


def _clone(task: dict, work: str) -> str | None:
    """Read-only worktree at the task's base commit, cached per (repo, commit)."""
    key = f"{str(task['repo']).replace('/', '__')}@{str(task['base_commit'])[:12]}"
    dest = os.path.join(work, key)
    if os.path.isdir(os.path.join(dest, ".git")):
        return dest
    mirror = os.path.join(work, "_mirrors", str(task["repo"]).replace("/", "__") + ".git")
    os.makedirs(os.path.dirname(mirror), exist_ok=True)
    env = dict(os.environ)
    try:
        if not os.path.isdir(mirror):
            # NOT blobless: --filter=blob:none leaves the worktree checkout silently INCOMPLETE
            # (the 50-run's first 7 tasks produced empty workspaces and therefore zero retrieval —
            # a code bug that read as "the agent found nothing"). Full mirror, cached per repo.
            subprocess.run(["git", "clone", "--bare", str(task["repo_url"]), mirror],
                           check=True, env=env, capture_output=True, timeout=2400)
        os.makedirs(dest, exist_ok=True)
        subprocess.run(["git", "clone", "--shared", "--no-checkout", mirror, dest],
                       check=True, env=env, capture_output=True, timeout=900)
        subprocess.run(["git", "-C", dest, "checkout", "--detach", str(task["base_commit"])],
                       check=True, env=env, capture_output=True, timeout=900)
        # INTEGRITY GATE: a partial checkout must never become a measured "found nothing" row.
        missing = subprocess.run(["git", "-C", dest, "status", "--porcelain"],
                                 env=env, capture_output=True, text=True, timeout=300).stdout
        deleted = sum(1 for line in missing.splitlines() if line.startswith(" D") or line.startswith("D "))
        if deleted:
            raise RuntimeError(f"incomplete checkout: {deleted} files missing from the worktree")
        return dest
    except Exception as exc:  # noqa: BLE001 — a task that will not assemble is SKIPPED, never faked
        print(f"  clone failed: {type(exc).__name__}: {str(exc)[:120]}", file=sys.stderr)
        shutil.rmtree(dest, ignore_errors=True)
        return None


def _rel(path: str, root: str) -> str:
    p = str(path or "")
    if p.startswith(root):
        p = p[len(root):]
    return p.lstrip("/")


def _spans_from_slice(rendered: str, root: str) -> dict[str, list[list[int]]]:
    """RESIDENT ledger: files the slice pushed into context, with their rendered line ranges."""
    out: dict[str, list[list[int]]] = {}
    for m in _SLICE_FILE.finditer(rendered or ""):
        path = _rel(m.group(1), root)
        if m.group(2):
            out.setdefault(path, []).append([1, int(m.group(2))])
        else:
            out.setdefault(path, []).append([int(m.group(3)), int(m.group(4))])
    return out


def _run_one(task: dict, workspace: str, model: str, max_steps: int) -> dict:
    """One read-only retrieval turn. Returns both ledgers plus accounting."""
    from sliceagent_cli.code_index import make_code_index
    from sliceagent_cli.code_grep import make_grep_tool
    from sliceagent_cli.coding_tool_host import LocalToolHost
    from sliceagent_core.events import SliceBuilt, ToolResult, make_dispatcher
    from sliceagent_core.llm import OpenAILLM
    from sliceagent_core.loop import run_turn
    from sliceagent_core.pfc import Slice, record_user, slice_sink
    from sliceagent_core.seed import make_build_slice

    prompt = (
        "Locate the code that must be understood to fix this issue. Read the files and regions you "
        "need; do NOT edit anything. When you have found them, answer with the file paths and line "
        "ranges that a correct fix depends on.\n\n=== ISSUE ===\n"
        + str(task.get("problem_statement") or "")[:12000]
    )
    state = Slice(); state.reset(prompt)
    tools = LocalToolHost(workspace)
    tools.registry.register(make_grep_tool(tools))
    retriever = make_code_index(workspace)

    pulled: dict[str, list[list[int]]] = {}
    steps: list[dict] = []
    slices: list[str] = []

    def _cap(e):
        if isinstance(e, ToolResult):
            if e.name not in _READ_TOOLS or e.failing:
                return
            out = str(e.output or "")
            spans: dict[str, list[list[int]]] = {}
            if e.name == "grep":
                # every hit line is real retrieved content, at its exact line number
                for path, line in _GREP_HIT.findall(out):
                    n = int(line)
                    spans.setdefault(_rel(path, workspace), []).append([n, n])
            else:
                path = _rel(str((e.args or {}).get("path") or ""), workspace)
                nums = [int(n) for n in _NUMBERED.findall(out)]
                if path and nums:   # the rendered numbered range IS the span the model saw
                    spans[path] = [[min(nums), max(nums)]]
            if not spans:
                return
            for path, sp in spans.items():
                pulled.setdefault(path, []).extend(sp)
            steps.append({"files": sorted(spans), "spans": spans})
        elif isinstance(e, SliceBuilt):
            slices.append(str(e.rendered or ""))

    dispatch = make_dispatcher(slice_sink(state), _cap)
    llm = OpenAILLM(model=model)
    record_user(state, prompt)
    build = make_build_slice(state, tools, retriever, None, prompt, model_id=model)
    t0 = time.time()
    result = run_turn(build_slice=build, llm=llm, tools=tools, dispatch=dispatch,
                      max_steps=max_steps)
    resident = dict(pulled)
    for rendered in slices:
        for path, spans in _spans_from_slice(rendered, workspace).items():
            resident.setdefault(path, []).extend(spans)
    return {
        "pulled": pulled, "resident": resident, "steps": steps,
        # TurnOutcome fields (execution.py): status / steps / usage / message — NOT `reason`.
        # The smoke run's silent zero-capture was this attribute name: the AttributeError was
        # swallowed by the per-task guard and read as "task failed".
        "answer": str(getattr(result, "message", "") or "")[:4000],
        "stop": str(getattr(result, "status", "") or ""),
        "wall_s": round(time.time() - t0, 1),
        "n_steps": int(getattr(result, "steps", 0) or 0),
    }


def _obj_spans(spans: dict[str, list[list[int]]]) -> dict[str, list[dict]]:
    """The scorer's span shape is {"start": n, "end": n} OBJECTS, not [start, end] pairs
    (parsers/trajectory.py:43 indexes span['start']). Convert at the projection boundary so the
    capture code stays in the cheap pair form."""
    return {path: [{"start": int(s), "end": int(e)} for s, e in sp] for path, sp in spans.items()}


def _pred_row(task: dict, ledger: dict[str, list[list[int]]], steps: list[dict]) -> dict:
    return {
        "instance_id": task["instance_id"],
        "traj_data": {
            "pred_steps": [{"files": st["files"], "spans": _obj_spans(st["spans"])} for st in steps],
            "pred_files": sorted(ledger.keys()),
            "pred_spans": _obj_spans(dict(sorted(ledger.items()))),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True, help="contextbench_verified.parquet")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--language", default="python", help="'' for all languages")
    ap.add_argument("--model", default=os.environ.get("AGENT_MODEL", "deepseek-v4-flash"))
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--work", default=os.path.expanduser("~/.cache/contextbench-repos"))
    ap.add_argument("--out", default=os.path.join(_REPO, "evals", "contextbench", "run1"))
    a = ap.parse_args()

    tasks = _load_tasks(a.data, a.n, a.language)
    os.makedirs(a.out, exist_ok=True); os.makedirs(a.work, exist_ok=True)
    raw_path = os.path.join(a.out, "raw.jsonl")
    done = set()
    if os.path.exists(raw_path):   # RESUMABLE (h2h convention): skip finished instances
        for line in open(raw_path, encoding="utf-8"):
            try:
                done.add(json.loads(line)["instance_id"])
            except Exception:  # noqa: BLE001
                continue
    print(f"tasks: {len(tasks)} ({len(done)} already done) · model={a.model}")

    for i, task in enumerate(tasks, 1):
        iid = task["instance_id"]
        if iid in done:
            continue
        print(f"[{i}/{len(tasks)}] {iid} ({task['repo']})", flush=True)
        workspace = _clone(task, a.work)
        if workspace is None:
            continue
        try:
            run = _run_one(task, workspace, a.model, a.max_steps)
        except Exception as exc:  # noqa: BLE001 — one bad task never kills the sweep
            import traceback
            print(f"  run failed: {type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr)
            traceback.print_exc(limit=3, file=sys.stderr)   # never let a code bug read as a task failure
            continue
        with open(raw_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"instance_id": iid, "repo": task["repo"], **run},
                               ensure_ascii=False) + "\n")
        print(f"  {run['n_steps']} steps · {run['wall_s']}s · pulled {len(run['pulled'])} files "
              f"· resident {len(run['resident'])}", flush=True)

    # project the two prediction files the official harness scores
    if not os.path.exists(raw_path):
        print("no completed tasks — nothing to project", file=sys.stderr)
        return 1
    rows = [json.loads(line) for line in open(raw_path, encoding="utf-8") if line.strip()]
    by_id = {t["instance_id"]: t for t in tasks}
    for ledger in ("pulled", "resident"):
        out = os.path.join(a.out, f"pred_{ledger}.jsonl")
        with open(out, "w", encoding="utf-8") as f:
            for r in rows:
                task = by_id.get(r["instance_id"])
                if task is None:
                    continue
                f.write(json.dumps(_pred_row(task, r[ledger], r["steps"]),
                                   ensure_ascii=False) + "\n")
        print(f"wrote {out} ({len(rows)} rows)")
    walls = [r["wall_s"] for r in rows]
    if walls:
        print(f"wall: total {sum(walls) / 60:.1f} min · median {sorted(walls)[len(walls) // 2]:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
