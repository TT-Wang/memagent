"""Robust overnight runner: sliceagent + codex over all 56 TB2.0 tasks, in parallel, retrying only the
NOT-yet-completed tasks each round (survives harbor/docker crashes), then auto-writes comparison.md/csv.
Run (key sourced):  set -a; source "/Users/tongtao/Desktop/agent design/.env"; set +a
  PYTHONPATH=src:. .venv/bin/python evals/tbench/run_all.py
"""
import glob
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
TASKS = [t for t in open(os.path.join(HERE, "tasks56.txt")).read().split() if t]
N_CONCURRENT = 2
MAX_ROUNDS = 4
CHUNK = 5                 # tasks per harbor invocation — small so images don't fill the disk
CHUNK_TIMEOUT = 5400      # 1.5h per chunk; a wedged chunk bails and the remainder retries


def _prune_images():
    try:
        subprocess.run(["docker", "image", "prune", "-af"], capture_output=True, timeout=180)
    except Exception:  # noqa: BLE001
        pass


def done_tasks(base: str) -> set:
    """A task is DONE only if some trial produced a real verdict (reward not None). Errored trials
    (timeout / env-build / setup failures) leave a result.json with reward=None → they get RETRIED."""
    from evals.tbench.compare import infra_failed
    d = set()
    for rf in glob.glob(os.path.join(base, "*", "*", "result.json")):
        td = os.path.dirname(rf)
        name = os.path.basename(td)
        if "__" not in name:
            continue
        try:
            r = (json.load(open(rf)).get("verifier_result") or {}).get("rewards", {}).get("reward")
        except Exception:  # noqa: BLE001
            r = None
        if r is not None and not infra_failed(td):  # infra/verifier failures get retried, not counted
            d.add(name.split("__")[0])
    return d


def run_agent(name: str, imp: str, base: str, env: dict) -> None:
    os.makedirs(base, exist_ok=True)
    for rnd in range(MAX_ROUNDS):
        remaining = [t for t in TASKS if t not in done_tasks(base)]
        if not remaining:
            print(f"[{name}] ALL {len(TASKS)} DONE", flush=True)
            return
        print(f"[{name}] round {rnd}: {len(remaining)} remaining (e.g. {remaining[:4]})", flush=True)
        for i in range(0, len(remaining), CHUNK):
            chunk = remaining[i:i + CHUNK]
            cmd = [os.path.join(ROOT, ".venv/bin/harbor"), "run", "--path", os.path.join(HERE, "tb2"),
                   "--agent-import-path", imp, "-m", "gpt-5.5", "-n", str(N_CONCURRENT), "--jobs-dir", base,
                   "--agent-setup-timeout-multiplier", "8",
                   "--environment-build-timeout-multiplier", "3",
                   "--agent-timeout-multiplier", "1.5"]
            for t in chunk:
                cmd += ["-i", t]
            print(f"[{name}] r{rnd} chunk {chunk}", flush=True)
            try:
                subprocess.run(cmd, env=env, cwd=ROOT, timeout=CHUNK_TIMEOUT)
            except Exception as e:  # noqa: BLE001 — a wedged chunk bails; remainder retries next round
                print(f"[{name}] chunk ended early: {type(e).__name__}: {e}", flush=True)
            _prune_images()  # free this chunk's images before the next — keeps disk from filling
    rem = [t for t in TASKS if t not in done_tasks(base)]
    print(f"[{name}] FINAL: {len(TASKS) - len(rem)}/{len(TASKS)} done; missing: {rem}", flush=True)


def main():
    base_env = dict(os.environ)
    base_env["PYTHONPATH"] = "src:."
    for p in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "all_proxy", "ALL_PROXY"):
        base_env.pop(p, None)

    mem_env = dict(base_env)
    mem_env["LLM_API_KEY"] = base_env.get("OPENAI_API_KEY", "")
    mem_env["AGENT_MODEL"] = "gpt-5.5"
    cod_env = dict(base_env)

    # SEQUENTIAL (sliceagent then codex): the box can't hold many task images, and we prune between chunks,
    # so running both at once would race on image teardown. Sequential keeps disk usage clean + bounded.
    run_agent("sliceagent", "evals.tbench.harbor_agent:SliceagentHarborAgent",
              os.path.join(HERE, "jobs", "sliceagent"), mem_env)
    run_agent("codex", "evals.tbench.harbor_agent:CodexSubscriptionHarborAgent",
              os.path.join(HERE, "jobs", "codex"), cod_env)

    print("=== both agents finished — writing comparison ===", flush=True)
    subprocess.run([os.path.join(ROOT, ".venv/bin/python"), os.path.join(HERE, "compare.py")],
                   cwd=ROOT, env=base_env)
    print("=== run_all DONE ===", flush=True)


if __name__ == "__main__":
    main()
