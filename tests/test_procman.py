"""Background / long-running process tools (procman) — the live-handle gap the one-shot
sandbox can't express (servers, multi-minute builds). Plus run_command's raised timeout ceiling.
Deterministic, no model, no pytest. Run: PYTHONPATH=src python tests/test_procman.py
"""
import os
import re
import shlex
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent.tools import LocalToolHost  # noqa: E402

PY = shlex.quote(sys.executable)
CHECKS = []
def check(fn):
    CHECKS.append(fn)
    return fn


def _host():
    wd = tempfile.mkdtemp(prefix="proc-")
    return wd, LocalToolHost(root=wd)


def _wait_for(get_text, pattern, timeout=5.0):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = get_text()
        m = re.search(pattern, last)
        if m:
            return m, last
        time.sleep(0.05)
    return None, last


@check
def tools_registered():
    _, h = _host()
    names = {s["function"]["name"] for s in h.schemas()}
    for t in ("proc_start", "proc_poll", "proc_tail", "proc_wait", "proc_kill"):
        assert t in names, f"{t} not registered"
    rc = next(s for s in h.schemas() if s["function"]["name"] == "run_command")
    assert "timeout" in rc["function"]["parameters"]["properties"], "run_command timeout param missing"


@check
def start_poll_tail_kill():
    wd, h = _host()
    open(os.path.join(wd, "counter.py"), "w").write(
        "import time\nfor i in range(500):\n    print('tick', i, flush=True)\n    time.sleep(0.02)\n")
    msg = h.run("proc_start", {"command": f"{PY} counter.py"})
    assert "p1" in msg, msg
    m, out = _wait_for(lambda: h.run("proc_tail", {"handle": "p1"}), r"tick \d", 3)
    assert m, f"no tail output: {out!r}"
    assert "running" in h.run("proc_poll", {"handle": "p1"})
    k = h.run("proc_kill", {"handle": "p1"})
    assert "killed p1" in k, k
    assert "exited" in h.run("proc_poll", {"handle": "p1"})


@check
def wait_short_then_done():
    wd, h = _host()
    open(os.path.join(wd, "sleeper.py"), "w").write(
        "import time\ntime.sleep(0.5)\nprint('DONE', flush=True)\n")
    h.run("proc_start", {"command": f"{PY} sleeper.py"})
    early = h.run("proc_wait", {"handle": "p1", "timeout": 0.1})
    assert "running" in early, f"expected still-running: {early!r}"
    late = h.run("proc_wait", {"handle": "p1", "timeout": 3})
    assert "exited 0" in late and "DONE" in late, f"expected exited+DONE: {late!r}"


@check
def poll_does_not_equate_leader_exit_with_group_extinction():
    if os.name != "posix":
        return
    wd, h = _host()
    h.run("proc_start", {"command": "sleep 30 & exit 0"})
    deadline = time.time() + 3
    status = ""
    while time.time() < deadline:
        status = h.run("proc_poll", {"handle": "p1"})
        if status.startswith("leader exited"):
            break
        time.sleep(0.05)
    try:
        assert "descendants running" in status, status
    finally:
        h.run("proc_kill", {"handle": "p1"})


@check
def server_start_probe_kill():
    """The canonical 'start a server, keep it alive, probe it' flow — impossible with one-shot run."""
    wd, h = _host()
    open(os.path.join(wd, "hello.txt"), "w").write("OK")
    # Pick a free port up front, then probe the LIVE HTTP ENDPOINT — not the server's stdout banner. Some CI
    # sandboxes (GitHub's macOS runner) don't surface a background process's stdout to proc_tail, so depending
    # on captured output is flaky; the served file is the real source of truth that the process is alive.
    import socket
    _s = socket.socket()
    _s.bind(("127.0.0.1", 0))
    port = _s.getsockname()[1]
    _s.close()
    h.run("proc_start", {"command": f"{PY} -u -m http.server {port} --bind 127.0.0.1"})
    body = None
    for _ in range(150):                       # up to ~15s for a cold runner to bind + serve
        try:
            body = urllib.request.urlopen(f"http://127.0.0.1:{port}/hello.txt", timeout=1).read().decode()
            break
        except Exception:  # noqa: BLE001 — server may not be accepting yet
            time.sleep(0.1)
    tail = h.run("proc_tail", {"handle": "p1"})   # exercise proc_tail (content not asserted — sandbox-dependent)
    h.run("proc_kill", {"handle": "p1"})
    assert body == "OK", f"server did not start/serve within ~15s: body={body!r} tail={tail!r}"
    assert "exited" in h.run("proc_poll", {"handle": "p1"})


@check
def run_command_timeout_arg():
    wd, h = _host()
    assert h.run("run_command", {"command": "echo hi", "timeout": 5}).strip() == "hi"
    slow = h.run("run_command", {"command": f'{PY} -c "import time; time.sleep(2)"', "timeout": 1})
    assert "124" in slow or "timed out" in slow, f"short timeout should trip: {slow!r}"


@check
def unknown_handle_errors():
    _, h = _host()
    out = h.run("proc_poll", {"handle": "pX"})
    assert "handle" in out.lower(), out


@check
def cleanup_kills_all():
    wd, h = _host()
    open(os.path.join(wd, "s.py"), "w").write("import time\ntime.sleep(60)\n")
    h.run("proc_start", {"command": f"{PY} s.py"})
    h.run("proc_start", {"command": f"{PY} s.py"})
    assert "running" in h.run("proc_poll", {"handle": "p1"})
    h.procs.cleanup()
    assert "handle" in h.run("proc_poll", {"handle": "p1"}).lower()


# ── the deadline is a TOOL CHOICE, not a verdict ────────────────────────────────────────────────
# A timeout that reports only "Exit code 124" reads as a dead end, and the next step is a blind retry
# at the same limit. Both blocking runners must instead hand back the escalation (raise timeout →
# proc_start) so long work can always finish somewhere.
@check
def timeout_result_carries_the_escalation():
    wd, h = _host()
    open(os.path.join(wd, "slow.py"), "w").write("import time\ntime.sleep(30)\n")
    for name, args in (("run_command", {"command": f"{PY} slow.py", "timeout": 1}),
                       ("execute_code", {"code": f"print(run({PY!r} + ' slow.py'))", "timeout": 1})):
        out = h.run(name, args)
        assert "124" in out, f"{name}: lost the timeout sentinel — {out!r}"
        assert "proc_start" in out, f"{name}: no escalation to the unbounded runner — {out!r}"
        assert getattr(out.status, "value", out.status) == "indeterminate", (
            f"{name}: a reaped tree is INDETERMINATE, never failed — {out.status!r}")


@check
def execute_code_honours_a_raised_deadline():
    """The regression that motivated this: execute_code is the MULTI-EDIT tool, so a hard 30s cap can
    land between two edits with no way for the caller to widen it."""
    wd, h = _host()
    code = "import time\ntime.sleep(2)\nwrite_file('done.txt', 'ok')\nprint('finished')"
    assert "finished" in h.run("execute_code", {"code": code, "timeout": 60})
    assert open(os.path.join(wd, "done.txt")).read() == "ok"
    # …and the ceiling still holds: no caller can ask for an unbounded blocking call.
    assert h._call_timeout(10**9) == 600.0 and h._call_timeout("nonsense") == float(h.timeout)


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
