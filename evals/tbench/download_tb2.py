"""Download TB2.0 task dirs from Hugging Face via curl + the /resolve endpoint (LFS-aware) — the hf_hub
client and git transport are both broken in this env, but curl to the HTTP API/resolve works.
Usage: python evals/tbench/download_tb2.py [task ...]   (default: all in tasks56.txt)
"""
import json
import os
import subprocess
import sys

REPO = "harborframework/terminal-bench-2.0"
API = f"https://huggingface.co/api/datasets/{REPO}/tree/main"
RESOLVE = f"https://huggingface.co/datasets/{REPO}/resolve/main"
OUT = os.path.join(os.path.dirname(__file__), "tb2")


_RETRY = ["--retry", "6", "--retry-all-errors", "--retry-delay", "2", "-fS"]


def curl(url: str) -> bytes:
    return subprocess.run(["curl", "-sL", *_RETRY, url], capture_output=True).stdout


def tree(task: str):
    # the tree API intermittently returns empty; retry the parse a few times via fresh curls
    last = b""
    for _ in range(6):
        last = curl(f"{API}/{task}?recursive=true")
        if last.strip().startswith(b"["):
            return json.loads(last.decode("utf-8", "replace"))
    raise ValueError(f"empty tree after retries: {last[:80]!r}")


def main():
    here = os.path.dirname(__file__)
    tasks = sys.argv[1:] or [l.strip() for l in open(os.path.join(here, "tasks56.txt")) if l.strip()]
    for t in tasks:
        try:
            entries = tree(t)
            files = [e["path"] for e in entries if e.get("type") == "file"]
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL tree {t}: {e}"); continue
        n = 0
        for p in files:
            local = os.path.join(OUT, p)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            subprocess.run(["curl", "-sL", f"{RESOLVE}/{p}", "-o", local])
            n += 1
        print(f"  {t}: {n} files")
    print("done")


if __name__ == "__main__":
    main()
