"""sliceagent vs codex TB2.0 comparison — per-task reward, agent wall-time, tokens (in/out), steps.
Works on partial/in-progress runs. Reads each trial's result.json (+ agent/metrics.json fallback).
Writes comparison.md + comparison.csv next to this file, and prints a summary.
Usage: python evals/tbench/compare.py [sliceagent_jobs_base] [codex_jobs_base]
"""
import csv
import glob
import json
import os
import sys
from datetime import datetime


def latest_job(base: str):
    ds = sorted(d for d in glob.glob(os.path.join(base, "*/")) if os.path.isdir(d))
    return ds[-1] if ds else None


_INFRA_MARKERS = (
    "curl: command not found", "uvx: command not found", "No space left on device",
    "not enough free space", "Could not get lock", "Unable to locate package",
    "/root/.local/bin/env: No such file", "Cannot connect to the Docker daemon",
    "uv: command not found", "Temporary failure resolving",
)


def infra_failed(trial_dir: str) -> bool:
    """True if the VERIFIER itself couldn't run (apt/curl/uv/disk/network) — a false failure, not the
    agent's fault. Such trials should be re-run, and never counted as an agent loss."""
    for vf in ("verifier/test-stdout.txt", "verifier/test-stderr.txt"):
        p = os.path.join(trial_dir, vf)
        if os.path.exists(p):
            try:
                txt = open(p, errors="replace").read()
            except Exception:  # noqa: BLE001
                continue
            if any(m in txt for m in _INFRA_MARKERS):
                return True
    return False


def _dur(a, b):
    if not a or not b:
        return None
    try:
        fmt = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
        return round((fmt(b) - fmt(a)).total_seconds(), 1)
    except Exception:  # noqa: BLE001
        return None


def trial_metrics(trial_dir: str) -> dict:
    rf = os.path.join(trial_dir, "result.json")
    out = {"reward": None, "tokens_in": None, "tokens_out": None, "steps": None,
           "walltime": None, "error": None, "infra": infra_failed(trial_dir)}
    try:
        r = json.load(open(rf))
    except Exception:  # noqa: BLE001
        return out
    out["reward"] = (r.get("verifier_result") or {}).get("rewards", {}).get("reward")
    ar = r.get("agent_result") or {}
    out["tokens_in"] = ar.get("n_input_tokens")
    out["tokens_out"] = ar.get("n_output_tokens")
    out["steps"] = (ar.get("metadata") or {}).get("steps")
    out["walltime"] = _dur((r.get("agent_execution") or {}).get("started_at"),
                           (r.get("agent_execution") or {}).get("finished_at"))
    if r.get("exception_info"):
        out["error"] = str(r["exception_info"])[:80]
    mf = os.path.join(trial_dir, "agent", "metrics.json")
    if (out["tokens_in"] is None or out["steps"] is None) and os.path.exists(mf):
        try:
            m = json.load(open(mf))
            out["tokens_in"] = out["tokens_in"] if out["tokens_in"] is not None else m.get("tokens_in")
            out["tokens_out"] = out["tokens_out"] if out["tokens_out"] is not None else m.get("tokens_out")
            out["steps"] = out["steps"] if out["steps"] is not None else m.get("steps")
        except Exception:  # noqa: BLE001
            pass
    return out


def collect_all(base: str) -> dict[str, dict]:
    """Merge trials across ALL job rounds under base (retry wrapper writes one timestamp dir per round);
    a completed result (reward present) wins over an incomplete one, latest round wins on ties."""
    def _score(tm):  # which trial to keep: clean verdict > infra-tainted verdict > no verdict
        if tm.get("reward") is not None and not tm.get("infra"):
            return 2
        if tm.get("reward") is not None:
            return 1
        return 0

    res = {}
    for job in sorted(glob.glob(os.path.join(base, "*/"))):
        for td in glob.glob(os.path.join(job, "*", "")):
            td = td.rstrip("/")
            name = os.path.basename(td)
            if "__" not in name:
                continue
            task = name.split("__")[0]
            tm = trial_metrics(td)
            if task not in res or _score(tm) >= _score(res[task]):
                res[task] = tm
    return res


def _fmt(v, n=0):
    if v is None:
        return "-"
    return f"{v:,.{n}f}" if isinstance(v, (int, float)) else str(v)


def main():
    here = os.path.dirname(__file__)
    mbase = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "jobs", "sliceagent")
    cbase = sys.argv[2] if len(sys.argv) > 2 else os.path.join(here, "jobs", "codex")
    mem, cod = collect_all(mbase), collect_all(cbase)
    tasks = [t for t in open(os.path.join(here, "tasks56.txt")).read().split() if t]
    rows = []
    for t in tasks:
        m, c = mem.get(t, {}), cod.get(t, {})
        rows.append((t, m, c))

    # markdown
    md = ["# TB2.0 — sliceagent (gpt-5.5 API) vs codex (subscription)\n",
          "Reward 1=pass 0=fail. Wall = agent-phase seconds. Tokens = in/out. Steps = tool/agent steps.\n",
          "| task | mem rew | cdx rew | mem wall | cdx wall | mem tok(in/out) | cdx tok(in/out) | "
          "mem steps | cdx steps | flag |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    inferior, mem_done, cod_done, mem_pass, cod_pass = [], 0, 0, 0, 0
    mem_tok = cod_tok = mem_wall = cod_wall = 0.0
    env_fail = []
    for t, m, c in rows:
        mi, ci = m.get("infra"), c.get("infra")
        mr = None if mi else m.get("reward")  # effective reward: infra failures aren't a real verdict
        cr = None if ci else c.get("reward")
        if mr is not None:
            mem_done += 1
            mem_pass += mr == 1.0
            mem_tok += (m.get("tokens_in") or 0) + (m.get("tokens_out") or 0)
            mem_wall += m.get("walltime") or 0
        if cr is not None:
            cod_done += 1
            cod_pass += cr == 1.0
            cod_tok += (c.get("tokens_in") or 0) + (c.get("tokens_out") or 0)
            cod_wall += c.get("walltime") or 0
        if mi or ci:
            env_fail.append(t)
        flag = ""
        if mr is not None and cr is not None and mr < cr:
            flag = "**sliceagent INFERIOR**"
            inferior.append(t)
        mr = "ENV" if mi else mr
        cr = "ENV" if ci else cr
        md.append(f"| {t} | {_fmt(mr)} | {_fmt(cr)} | {_fmt(m.get('walltime'),0)} | "
                  f"{_fmt(c.get('walltime'),0)} | {_fmt(m.get('tokens_in'))}/{_fmt(m.get('tokens_out'))} | "
                  f"{_fmt(c.get('tokens_in'))}/{_fmt(c.get('tokens_out'))} | {_fmt(m.get('steps'))} | "
                  f"{_fmt(c.get('steps'))} | {flag} |")
    md += ["",
           f"**Completed:** sliceagent {mem_done}/{len(tasks)}, codex {cod_done}/{len(tasks)}",
           f"**Pass rate:** sliceagent {mem_pass}/{mem_done or 1} · codex {cod_pass}/{cod_done or 1}",
           f"**Total agent wall:** sliceagent {mem_wall/60:.0f}m · codex {cod_wall/60:.0f}m",
           f"**Total tokens:** sliceagent {mem_tok:,.0f} · codex {cod_tok:,.0f}",
           f"**sliceagent INFERIOR on {len(inferior)}:** {', '.join(inferior) or '(none)'}",
           f"**ENV/verifier-infra failures (need re-run, not a real verdict) on {len(set(env_fail))}:** "
           f"{', '.join(sorted(set(env_fail))) or '(none)'}"]
    open(os.path.join(here, "comparison.md"), "w").write("\n".join(md))

    # csv
    with open(os.path.join(here, "comparison.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["task", "mem_reward", "cdx_reward", "mem_wall_s", "cdx_wall_s",
                    "mem_tok_in", "mem_tok_out", "cdx_tok_in", "cdx_tok_out",
                    "mem_steps", "cdx_steps", "mem_error", "cdx_error"])
        for t, m, c in rows:
            w.writerow([t, m.get("reward"), c.get("reward"), m.get("walltime"), c.get("walltime"),
                        m.get("tokens_in"), m.get("tokens_out"), c.get("tokens_in"), c.get("tokens_out"),
                        m.get("steps"), c.get("steps"), m.get("error"), c.get("error")])

    print("\n".join(md[-5:]))
    print(f"\nwrote {os.path.join(here, 'comparison.md')} + comparison.csv")


if __name__ == "__main__":
    main()
