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


def _run_one(task: dict, workspace: str, model: str, max_steps: int,
             prompt_style: str = "baseline", memory_mode: str = "real") -> dict:
    """One read-only retrieval turn. Returns both ledgers plus accounting."""
    from sliceagent_cli.code_index import make_code_index
    from sliceagent_cli.code_grep import make_grep_tool
    from sliceagent_cli.coding_tool_host import LocalToolHost
    from sliceagent_core.events import SliceBuilt, StepEnd, ToolResult, make_dispatcher
    from sliceagent_core.llm import OpenAILLM
    from sliceagent_core.loop import run_turn
    from sliceagent_core.pfc import Slice, record_user, slice_sink
    from sliceagent_core.seed import make_build_slice

    # CONVERGENCE ARM (--prompt exhaustive): the baseline wording ("locate the code that must be
    # understood") produced 2.76x gold's line volume yet hit only 64% of gold FILES and an EditLoc
    # recall of 0.136 with 0.949 precision — a profile that is directionally right and badly
    # under-converged. This arm tests whether that is a WORDING artifact or a retrieval-ranking
    # property: same agent, same repo, same model, only the ask changes.
    if prompt_style == "exhaustive":
        prompt = (
            "Find EVERY location a correct fix for this issue must change or depend on — do not stop "
            "at the first plausible file. Work until you can name each one: read the definition AND "
            "its call sites, follow imports and subclasses, and check tests that pin the behaviour. "
            "Do NOT edit anything. Answer with a COMPLETE list of file paths and line ranges, one per "
            "line; include every location you would need to touch, not just the most likely."
            "\n\n=== ISSUE ===\n" + str(task.get("problem_statement") or "")[:12000]
        )
    else:
        prompt = (
            "Locate the code that must be understood to fix this issue. Read the files and regions you "
            "need; do NOT edit anything. When you have found them, answer with the file paths and line "
            "ranges that a correct fix depends on.\n\n=== ISSUE ===\n"
            + str(task.get("problem_statement") or "")[:12000]
        )
    state = Slice(); state.reset(prompt)
    tools = LocalToolHost(workspace)
    tools.registry.register(make_grep_tool(tools))
    # PRODUCTION ACTIVE-WORK PARITY (the same defect benchmarks/run.py carried until the cost
    # review): with no bound provider, LocalToolHost.schemas() keeps the six legacy semantic-state
    # tools AND injects the 351-char `note` arg into EVERY schema — 12,697 chars/call that the CLI
    # (cli.py:_bind_active_work_host) never pays. Binding alone is schema reduction, not parity, so
    # the turn below is admitted with stable ids too: without a request ROOT, update_work stays
    # advertised in a state where it errors ("no active request root").
    # Measured single-fix effect (run-tape-parity-2026-08-05, same 50 tasks vs the unbound arm):
    # cost $0.3514 -> $0.3308, median peak 32,377 -> 26,022, fileF1 0.579 -> 0.609.
    if callable(getattr(tools, "bind_active_work", None)):
        tools.bind_active_work(lambda: (state.active_work, "L1", 0))
    retriever = make_code_index(workspace)
    # PRODUCTION PARITY (same defect as benchmarks/run.py): the original arm passed memory=None, so
    # the model never even SAW the search_history tool or the manifest region — a different tool
    # surface from production, silently. Single-turn means the archive is empty (as in any real
    # first turn), but surface parity is still parity. "null" stays available as the ablation.
    import tempfile as _tf
    session_id = f"cb-{task['instance_id'][-16:]}-{os.getpid()}"
    memory = None
    telem = None
    extra_sinks = []
    if memory_mode == "real":
        os.environ["SLICEAGENT_VAULT"] = _tf.mkdtemp(prefix="cb-vault-")
        from sliceagent_cli.hippocampus import EpisodeSink, HistoryFS, make_search_history_tool
        from sliceagent_cli.memory import LocalMemory
        from sliceagent_cli.telemetry import make_telemetry_sink
        memory = LocalMemory(prefer_memem=False)
        telem = make_telemetry_sink()
        extra_sinks = [EpisodeSink(memory, session_id=session_id, task_id_fn=lambda: "t-cb",
                                   title_fn=lambda: task["instance_id"][-24:], outcome_fn=lambda: {}),
                       telem]
        tools._history = HistoryFS(memory, session_id)
        tools.registry.register(make_search_history_tool(memory, session_id))

    pulled: dict[str, list[list[int]]] = {}
    steps: list[dict] = []
    slices: list[str] = []
    # INDEPENDENT LEDGER (extraction self-audit): `pulled` is derived by REGEX over the rendered
    # tool output; this one reads the host's TYPED resource_observed ToolEffect payload
    # (coding_tool_host._read_resource_effects — resource_kind/handle/sha256, no text parsing).
    # Two derivations that share no code path. Agreement on the file set is the evidence that the
    # self-authored extractor neither over- nor under-counts; spans exist only on the text path, so
    # the audit covers FILES, not line ranges — stated, not glossed.
    typed: set = set()

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
            for eff in (getattr(getattr(e, "outcome", None), "effects", ()) or ()):
                if getattr(eff, "kind", "") == "resource_observed":
                    handle = str((getattr(eff, "payload", {}) or {}).get("handle") or "")
                    kind = str((getattr(eff, "payload", {}) or {}).get("resource_kind") or "")
                    if handle and kind == "workspace_file":
                        typed.add(_rel(handle, workspace))
            if not spans:
                return
            for path, sp in spans.items():
                pulled.setdefault(path, []).extend(sp)
            steps.append({"files": sorted(spans), "spans": spans})
        elif isinstance(e, SliceBuilt):
            slices.append(str(e.rendered or ""))

    # COST AXIS. The control arm's trajectory carries real per-call provider usage, so this arm has to
    # report the SAME quantities from the same source (the provider's own numbers) rather than an
    # estimate. Deriving token counts from rendered character counts is not good enough: a chars/4
    # estimate of the mini arm understated its real peak by 38% (47.3k vs 76.3k measured) and its
    # cumulative input by 32% (1.46M vs 2.14M), which would have made every efficiency ratio wrong in
    # our own favour. StepEnd.usage is the provider breakdown (llm._usage_dict), so peak is a real
    # largest-single-call figure and the cache split is real, not inferred.
    usage_acc = {"calls": 0, "peak_in": 0, "in_total": 0, "in_cached": 0, "out_total": 0}

    def _usage(e):
        if isinstance(e, StepEnd):
            u = e.usage or {}
            p = int(u.get("prompt_tokens", 0) or 0)
            usage_acc["calls"] += 1
            usage_acc["in_total"] += p
            usage_acc["peak_in"] = max(usage_acc["peak_in"], p)
            usage_acc["in_cached"] += int(u.get("input_cache_read", 0) or 0)
            usage_acc["out_total"] += int(u.get("completion_tokens", 0) or 0)

    dispatch = make_dispatcher(slice_sink(state), _cap, _usage, *extra_sinks)
    # Endpoint resolution parity with benchmarks/run.py (dead-run incident 2026-08-05: a bare
    # OpenAILLM(model=...) fell through to the DEFAULT endpoint and 404'd every task in ~1s —
    # 50 silent one-step no-ops that read as a finished sweep). Env wins; config supplies the
    # provider base_url/key otherwise.
    if not (os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_API_KEY")):
        from sliceagent.config import load_config
        _cfg = load_config()
        _prov = (_cfg.providers() or {}).get("deepseek") if "deepseek" in str(model) else None
        llm = OpenAILLM(model=model,
                        api_key=os.environ.get("LLM_API_KEY") or (_prov or {}).get("api_key") or _cfg.api_key,
                        base_url=os.environ.get("LLM_BASE_URL") or (_prov or {}).get("base_url") or _cfg.base_url or None)
    else:
        llm = OpenAILLM(model=model)
    record_user(state, prompt, source_artifact="turn-001", source_event_id="ev-001",
                source_text=prompt, logical_id="L1", workspace_epoch=0)
    build = make_build_slice(state, tools, retriever, memory, prompt, session_id, model_id=model)
    t0 = time.time()
    result = run_turn(build_slice=build, llm=llm, tools=tools, dispatch=dispatch,
                      max_steps=max_steps)
    resident = dict(pulled)
    for rendered in slices:
        for path, spans in _spans_from_slice(rendered, workspace).items():
            resident.setdefault(path, []).extend(spans)
    return {
        "pulled": pulled, "resident": resident, "steps": steps,
        "typed_files": sorted(typed),
        # TurnOutcome fields (execution.py): status / steps / usage / message — NOT `reason`.
        # The smoke run's silent zero-capture was this attribute name: the AttributeError was
        # swallowed by the per-task guard and read as "task failed".
        "answer": str(getattr(result, "message", "") or "")[:4000],
        "stop": str(getattr(result, "status", "") or ""),
        "wall_s": round(time.time() - t0, 1),
        "n_steps": int(getattr(result, "steps", 0) or 0),
        "usage": dict(usage_acc),
        "memory_mode": memory_mode,
        "episodes_written": (len(memory.episode_manifest(session_id, 50)[0])
                             if memory is not None else None),
        **({"recalls": telem.summary()["recalls"], "re_reads": telem.summary()["re_reads"]}
           if telem is not None else {"recalls": None, "re_reads": None}),
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
    ap.add_argument("--memory", choices=("real", "null"), default="real",
                    help="real = production tool surface (archive+search_history, isolated vault); "
                         "null = the legacy ablation without the recall surface")
    ap.add_argument("--prompt", choices=("baseline", "exhaustive"), default="baseline",
                    help="convergence arm: 'exhaustive' asks for a COMPLETE location list")
    ap.add_argument("--work", default=os.path.expanduser("~/.cache/contextbench-repos"))
    ap.add_argument("--out", default=os.path.join(_REPO, "evals", "contextbench", "run1"))
    ap.add_argument("--instances", default="",
                    help="comma-separated instance_id suffixes; when set, run only matching tasks")
    a = ap.parse_args()

    tasks = _load_tasks(a.data, a.n, a.language)
    if a.instances:
        wanted = [s.strip() for s in a.instances.split(",") if s.strip()]
        tasks = [t for t in tasks if any(t["instance_id"].endswith(w) for w in wanted)]
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
            run = _run_one(task, workspace, a.model, a.max_steps, a.prompt, a.memory)
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
