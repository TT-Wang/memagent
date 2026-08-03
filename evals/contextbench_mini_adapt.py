"""Normalize mini-swe-agent 2.4.6 trajectories so ContextBench's OFFICIAL extractor can read them.

WHY THIS EXISTS — the apples-to-apples requirement.

ContextBench's mini extractor accepts two shapes:
  1. `<explore_context>` blocks — the model DECLARING which files/lines it used. Those blocks come
     from ContextBench's own patched agent (agent-frameworks/.../agents/context_aware.py regexes
     `<EXPLORE_CONTEXT>` out of the model's reply and validates its `File:`/`Lines:` format).
  2. a fallback that parses ```bash blocks out of assistant CONTENT — i.e. OBSERVED commands.

Those two measure different things. A self-declared ledger can be curated after the fact (read
eleven files, declare the three that mattered) and so reports a flattering precision; an observed
ledger records every footprint including dead-end exploration. sliceagent's ledger is OBSERVED
(we read its real tool calls), so comparing it against a declared ledger is not a fair test in
either direction — it is a different measurement.

So this arm uses VANILLA mini (no context patch, no declaration prompt) and repairs only the
FORMAT mismatch that made the observed-path fallback fail: mini 2.4.6 emits its commands as
`tool_calls` (assistant content empty), while the fallback looks for ```bash fences inside that
content. This module moves each bash tool call into the content as a fenced block — changing the
ENCODING, never the evidence. Nothing is added that the agent did not actually run.

  .venv/bin/python -m evals.contextbench_mini_adapt --in <dir-of-.traj.json> --out <dir>
"""
from __future__ import annotations

import argparse
import glob
import json
import os


def adapt(traj: dict) -> tuple[dict, int]:
    """Return (adapted trajectory, number of bash commands surfaced into assistant content)."""
    out = dict(traj)
    messages = []
    moved = 0
    for msg in traj.get("messages") or []:
        m = dict(msg)
        if m.get("role") == "assistant" and not str(m.get("content") or "").strip():
            fences = []
            for call in m.get("tool_calls") or []:
                fn = (call or {}).get("function") or {}
                if str(fn.get("name") or "") != "bash":
                    continue
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:  # noqa: BLE001 — a malformed call is skipped, never invented
                    continue
                cmd = str(args.get("command") or "").strip()
                if cmd:
                    fences.append(f"```bash\n{cmd}\n```")
                    moved += 1
            if fences:
                m["content"] = "\n\n".join(fences)
        messages.append(m)
    out["messages"] = messages
    return out, moved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="src", required=True, help="directory of mini .traj.json files")
    ap.add_argument("--out", dest="dst", required=True)
    a = ap.parse_args()
    os.makedirs(a.dst, exist_ok=True)
    total = files = empty = 0
    for path in sorted(glob.glob(os.path.join(a.src, "*.traj.json"))):
        try:
            traj = json.load(open(path, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"skip {os.path.basename(path)}: {type(exc).__name__}")
            continue
        adapted, moved = adapt(traj)
        if moved == 0:
            empty += 1   # a trajectory with no bash calls is REPORTED, never silently written as ok
        with open(os.path.join(a.dst, os.path.basename(path)), "w", encoding="utf-8") as f:
            json.dump(adapted, f, ensure_ascii=False)
        total += moved
        files += 1
    print(f"adapted {files} trajectories · {total} bash commands surfaced · "
          f"{empty} with NO bash commands (those extract to nothing — check before scoring)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def project_predictions(adapted_dir: str, out_path: str) -> int:
    """Official extractor → ContextBench pred rows, with final context = UNION OF OBSERVED STEPS.

    The official mini extractor sources final context ONLY from `<PATCH_CONTEXT>` blocks, which
    vanilla (unpatched) mini never emits — so a faithful observed run would score an empty final
    set. sliceagent's rows are built the same way its ledger is defined: pred_files/pred_spans =
    the union of everything the agent actually retrieved along the trajectory. Applying that
    SAME rule to mini's officially-extracted steps is what keeps the two arms comparable; using
    each agent's native final-context convention would compare two different quantities.
    """
    import sys
    sys.path.insert(0, os.environ.get("CB_REPO", ""))
    from contextbench.agents import extract_trajectory

    rows = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for path in sorted(glob.glob(os.path.join(adapted_dir, "*.traj.json"))):
            instance_id = os.path.basename(path)[: -len(".traj.json")]
            try:
                ex = extract_trajectory(path)
            except Exception as exc:  # noqa: BLE001 — a task that cannot extract is SKIPPED, not zeroed
                print(f"  extract failed {instance_id[-14:]}: {type(exc).__name__}")
                continue
            steps = ex.get("pred_steps") or []
            union: dict[str, list] = {}
            for st in steps:
                for path_, spans in (st.get("spans") or {}).items():
                    union.setdefault(path_, []).extend(spans)
            out.write(json.dumps({
                "instance_id": instance_id,
                "traj_data": {"pred_steps": steps,
                              "pred_files": sorted(union),
                              "pred_spans": {k: v for k, v in sorted(union.items())}},
            }, ensure_ascii=False) + "\n")
            rows += 1
    print(f"wrote {out_path} ({rows} rows; final = union of officially-extracted steps)")
    return rows
