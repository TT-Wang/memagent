"""LIVE validation that removing the interactive-PTY (terminal_*) and background-service (proc_*) harness
blocks actually works — the container-aware managers drive a REAL TTY and a REAL background process INSIDE
a throwaway Docker container (the same mechanism the TB adapter uses). No TB images / no LLM needed.

Run (needs Docker):  PYTHONPATH=src:. .venv/bin/python evals/tbench/validate_container_io.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import docker
from evals.tbench.agent import ContainerSessionManager, ContainerProcManager, ContainerToolHost

CWD = "/tmp"
results = []
def ok(name, cond, detail=""):
    results.append(cond); print(f"  {'PASS' if cond else 'FAIL'} {name}{(' — ' + detail) if detail else ''}")


def main():
    client = docker.from_env()
    print("# starting throwaway container (python:3.11-slim, sleep)…")
    c = client.containers.run("python:3.11-slim", ["sleep", "600"], detach=True, working_dir=CWD,
                              auto_remove=False)
    try:
        c.reload()
        # ---- BLOCK 1: interactive PTY inside the container (terminal_*) -----------------------------
        print("\n## Block 1 — interactive PTY (terminal_*) via docker exec -it")
        term = ContainerSessionManager(c)
        term.open("sh", cwd=CWD)                                   # a real bash TTY in the container
        term.send("sh", "echo START $((6*7)) END")                # send keystrokes → live response
        got = term.wait("sh", r"START 42 END", timeout=12)
        ok("pty shell round-trip", "START 42 END" in got, got.strip().replace("\n", " ")[-80:])
        # prove it's a genuine interactive program (a REPL), not just one-shot
        term.open("repl", cwd=CWD, command="python3 -i -q")
        term.send("repl", "print(2**10)")
        got2 = term.wait("repl", r"\b1024\b", timeout=12)
        ok("interactive python REPL", "1024" in got2, got2.strip().replace("\n", " ")[-80:])
        term.close("sh"); term.close("repl")
        ok("sessions close cleanly", term.list() == "(no terminal sessions)")

        # ---- BLOCK 2: persistent background service inside the container (proc_*) -------------------
        print("\n## Block 2 — background service (proc_*) via docker exec")
        procs = ContainerProcManager(c)
        h = procs.start("python3 -m http.server 8099", cwd=CWD)    # a server that never exits
        import time; time.sleep(1.5)
        ok("proc reports running", procs.poll(h) == "running", procs.poll(h))
        # probe the server from INSIDE the container — proves it's actually listening in there
        code, probe = c.exec_run(["python3", "-c",
            "import urllib.request as u; print(u.urlopen('http://localhost:8099').status)"], demux=False)
        pout = probe.decode() if isinstance(probe, (bytes, bytearray)) else str(probe)
        ok("server reachable in-container", "200" in pout, pout.strip())
        tail = procs.tail(h, 20)
        ok("proc_tail shows the request", "GET" in tail or "200" in tail, tail.strip().replace("\n", " ")[-90:])
        ok("proc_kill stops it", "killed" in procs.kill(h))

        # ---- wiring: ContainerToolHost exposes all 20 tools, bound to the container managers --------
        print("\n## Wiring — ContainerToolHost registers proc_*/terminal_* on container managers")
        host = ContainerToolHost(c, CWD)
        names = set(host.registry.names()) if hasattr(host.registry, "names") else {
            e.name for e in getattr(host.registry, "_tools", {}).values()}
        want = {"terminal_open", "terminal_send", "terminal_wait", "terminal_close", "terminal_read",
                "proc_start", "proc_poll", "proc_tail", "proc_wait", "proc_kill"}
        ok("all proc_*/terminal_* registered", want <= names, f"missing={sorted(want - names)}")
        ok("bound to container managers",
           isinstance(host.procs, ContainerProcManager) and isinstance(host.terminals, ContainerSessionManager))
        host.cleanup()
    finally:
        print("\n# tearing down container…")
        try: c.kill()
        except Exception: pass
        try: c.remove(force=True)
        except Exception: pass

    n_ok, n = sum(1 for r in results if r), len(results)
    print(f"\n==================== container I/O validation {n_ok}/{n} "
          f"{'PASS' if n_ok == n else 'FAIL'} ====================")
    sys.exit(0 if n_ok == n else 1)


if __name__ == "__main__":
    main()
