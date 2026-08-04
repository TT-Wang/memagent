"""Kimi Code arm for the ContextBench 50-task comparison.

Runs Kimi (model pinned per-invocation, e.g. deepseek/v4-flash) on the same 50 tasks, same
read-only prompt, same isolated per-task APFS-clone workspaces as the other two arms, and builds
its OBSERVED prediction ledger from the stream-json trajectory.

LEDGER DERIVATION — aligned with both existing arms, disclosed per channel:
  * Read {path, line_offset, n_lines}  -> exact span, like sliceagent's read_file numbered range
  * Grep result lines "path:NN:"       -> hit-line spans, the SAME crediting rule as sliceagent's
                                          grep channel (and droppable by the same reads-only rule)
  * Bash commands                      -> parsed with the SAME per-chunk rules as the patched
                                          official mini extractor (nl/sed/cat/head reads credited,
                                          mutating chunks skipped) so neither transcript arm gets
                                          a private convention
  * Glob                               -> names only, never credited (the list_files rule)

Escape audit: any Read/Grep path or Bash cwd outside the task workspace marks the task
contaminated, same gate as the mini arm.

  .venv/bin/python -m evals.contextbench_kimi run --n-workers 5     # all 50, resumable
  .venv/bin/python -m evals.contextbench_kimi extract --out pred_kimi.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

S = "/private/tmp/claude-501/-Users-tongtao-Desktop-agent-design/15414cfe-90d2-4130-a90a-aed29fd6e4fd/scratchpad"
KIMI = os.path.expanduser("~/.kimi-code/bin/kimi")
TASKS = "/tmp/mini_tasks.jsonl"          # same 50-task list the other arms consumed
TRAJS = os.path.join(S, "kimi_cb_trajs")
ISO = "/private/tmp/agentws"
PROMPT = ("Locate the code that must be understood to fix this issue. Read the files and regions "
          "you need; do NOT edit anything. When found, answer with the file paths and line ranges "
          "a correct fix depends on.\n\n=== ISSUE ===\n")
_HIT = re.compile(r"^([^\s:]+):(\d+):", re.M)


def _tasks():
    return [json.loads(l) for l in open(TASKS)]


def run(n_workers: int, worker: int) -> int:
    os.makedirs(TRAJS, exist_ok=True)
    for i, t in enumerate(_tasks()):
        if i % n_workers != worker:
            continue
        iid = t["instance_id"]
        out = os.path.join(TRAJS, f"{iid}.stream.jsonl")
        if os.path.exists(out):
            continue
        repo = t["repo"].replace("/", "__")
        src = os.path.expanduser(f"~/.cache/contextbench-repos/{repo}@{t['base_commit'][:12]}")
        if not os.path.isdir(os.path.join(src, ".git")):
            print(f"w{worker} [{i+1}] NO WORKSPACE {iid}")
            continue
        ws = os.path.join(ISO, f"kimi_{os.getpid()}_{i}")
        subprocess.run(["rm", "-rf", ws]); os.makedirs(ws, exist_ok=True)
        r = subprocess.run(["cp", "-Rc", src, os.path.join(ws, "repo")], capture_output=True)
        if r.returncode != 0:
            subprocess.run(["cp", "-R", src, os.path.join(ws, "repo")])
        t0 = time.time()
        try:
            proc = subprocess.run(
                [KIMI, "-m", "deepseek/v4-flash", "--output-format", "stream-json",
                 "-p", PROMPT + str(t["ps"])],
                cwd=os.path.join(ws, "repo"), capture_output=True, text=True, timeout=1200)
            body = proc.stdout
        except subprocess.TimeoutExpired:
            body = ""
            print(f"w{worker} [{i+1}] {iid[-14:]} TIMEOUT")
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
        subprocess.run(["rm", "-rf", ws])
        n_tools = body.count('"tool_calls"')
        print(f"w{worker} [{i+1}] {iid[-14:]} {round(time.time()-t0)}s tool_msgs~{n_tools}",
              flush=True)
    print(f"w{worker} done")
    return 0


def _bash_views(cmd: str):
    """Reuse the PATCHED official mini extractor for bash reads — one convention for both arms."""
    sys.path.insert(0, "/Users/tongtao/.slock/agents/0f836fc2-e9ae-43ad-822f-8c6a03d1ca94/task123-sources/contextbench")
    from contextbench.agents.minisweagent.extract import _extract_views_from_command
    return _extract_views_from_command(cmd) or []


def extract_one(path: str) -> tuple[dict, list, int]:
    """-> (spans_by_file, steps, escapes). Paths normalised repo-relative; absolute paths outside
    the task workspace count as escapes."""
    calls_by_id = {}
    steps = []
    union: dict[str, list] = {}
    escapes = 0

    def rel(p: str) -> str | None:
        nonlocal escapes
        p = (p or "").strip().strip("'\"")
        if not p:
            return None
        if p.startswith("/"):
            m = re.match(r"/private/tmp/agentws/[^/]+/repo/(.+)", p)
            if m:
                return m.group(1)
            escapes += 1
            return None
        return p.lstrip("./") or None

    def add(step_spans, f, lo, hi):
        f2 = rel(f)
        if f2:
            step_spans.setdefault(f2, []).append([int(lo), int(hi)])

    for ln in open(path, errors="ignore"):
        try:
            o = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if o.get("role") == "assistant":
            step_spans: dict[str, list] = {}
            for c in o.get("tool_calls") or []:
                fn = c.get("function") or {}
                try:
                    a = json.loads(fn.get("arguments") or "{}")
                except Exception:  # noqa: BLE001
                    continue
                name = fn.get("name")
                calls_by_id[c.get("id")] = name
                if name == "Read":
                    lo = int(a.get("line_offset") or 1)
                    n = int(a.get("n_lines") or 0)
                    add(step_spans, a.get("path") or a.get("file_path"), lo,
                        lo + max(n - 1, 0) if n else lo + 4000)
                elif name == "Bash":
                    for v in _bash_views(str(a.get("command") or "")):
                        f2 = rel(str(v.get("file") or ""))
                        if f2 and "start_line" in v:
                            step_spans.setdefault(f2, []).append(
                                [int(v["start_line"]), int(v["end_line"])])
                        elif f2:
                            step_spans.setdefault(f2, [])
            if step_spans:
                steps.append(step_spans)
        elif o.get("role") == "tool":
            if calls_by_id.get(o.get("tool_call_id")) == "Grep":
                gsp: dict[str, list] = {}
                for f, n in _HIT.findall(str(o.get("content") or "")):
                    f2 = rel(f)
                    if f2:
                        gsp.setdefault(f2, []).append([int(n), int(n)])
                if gsp:
                    steps.append(gsp)
    for st in steps:
        for f, sp in st.items():
            union.setdefault(f, []).extend(sp)
    return union, steps, escapes


def extract(out_path: str) -> int:
    import glob as g
    rows = esc_total = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for path in sorted(g.glob(os.path.join(TRAJS, "*.stream.jsonl"))):
            iid = os.path.basename(path)[: -len(".stream.jsonl")]
            union, steps, escapes = extract_one(path)
            esc_total += escapes
            pred_steps = [{"files": sorted(st), "spans":
                           {f: [{"start": a, "end": b} for a, b in sp] for f, sp in st.items()}}
                          for st in steps]
            out.write(json.dumps({
                "instance_id": iid,
                "traj_data": {
                    "pred_steps": pred_steps,
                    "pred_files": sorted(union),
                    "pred_spans": {f: [{"start": a, "end": b} for a, b in sp]
                                   for f, sp in sorted(union.items())}},
            }, ensure_ascii=False) + "\n")
            rows += 1
    print(f"wrote {out_path} ({rows} rows) · escape-path references: {esc_total} "
          f"(each is a read OUTSIDE the task workspace — must be 0 for a clean comparison)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--n-workers", type=int, default=1)
    r.add_argument("--worker", type=int, default=0)
    e = sub.add_parser("extract"); e.add_argument("--out", required=True)
    a = ap.parse_args()
    sys.exit(run(a.n_workers, a.worker) if a.cmd == "run" else extract(a.out))
