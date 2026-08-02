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
    open(os.path.join(wd, "counter.py"), "w", encoding="utf-8").write(
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
    open(os.path.join(wd, "sleeper.py"), "w", encoding="utf-8").write(
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
    open(os.path.join(wd, "hello.txt"), "w", encoding="utf-8").write("OK")
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
    # the deadline trips and the live process is adopted into the background registry
    assert "was NOT killed" in str(slow) and "background as p" in str(slow), \
        f"short timeout should trip into adoption: {str(slow)[:160]!r}"
    h.run("proc_kill", {"handle": re.search(r"background as (p\d+)", str(slow)).group(1)})


@check
def unknown_handle_errors():
    _, h = _host()
    out = h.run("proc_poll", {"handle": "pX"})
    assert "handle" in out.lower(), out


@check
def cleanup_kills_all():
    wd, h = _host()
    open(os.path.join(wd, "s.py"), "w", encoding="utf-8").write("import time\ntime.sleep(60)\n")
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
def a_deadline_hit_is_adopted_not_reaped_and_progress_survives():
    """FIELD (Kimi Code's autoBackgroundOnTimeout, sliceagent style): a 600s build was REAPED at the
    deadline, losing all progress — the escalation even said "use proc_start" after killing the work.
    Now the deadline ADOPTS the live process into the background registry: the result names the
    handle (never "reaped", never a failure verdict), and the process finishes on its own with its
    full output in the proc log."""
    wd, h = _host()
    open(os.path.join(wd, "slow.py"), "w", encoding="utf-8").write(
        "import time\nprint('phase-1', flush=True)\ntime.sleep(3)\nprint('phase-2-done', flush=True)\n")
    out = h.run("run_command", {"command": f"{PY} slow.py", "timeout": 1})
    text = str(out)
    assert "was NOT killed" in text and "background as p" in text, text[:200]
    assert "reaped" not in text and "Exit code 124" not in text, text[:200]
    handle = re.search(r"background as (p\d+)", text).group(1)
    # the adopted process keeps running past the deadline and finishes WITH its late output —
    # the reap path could never produce this line
    m, last = _wait_for(lambda: h.run("proc_tail", {"handle": handle}), r"exited 0", timeout=10)
    assert m, f"adopted process never completed: {last[:200]}"
    assert "phase-2-done" in last and "phase-1" in last, last[:300]


@check
def an_adopted_process_can_be_followed_and_killed_like_any_proc():
    wd, h = _host()
    open(os.path.join(wd, "s.py"), "w", encoding="utf-8").write("import time\ntime.sleep(30)\n")
    out = h.run("run_command", {"command": f"{PY} s.py", "timeout": 1})
    handle = re.search(r"background as (p\d+)", str(out)).group(1)
    assert "running" in h.run("proc_poll", {"handle": handle})
    killed = h.run("proc_kill", {"handle": handle})
    assert f"killed {handle}" in killed, killed
    assert "exited" in h.run("proc_poll", {"handle": handle}) or "killed" in killed


@check
def the_oracle_and_plain_shell_timeouts_keep_the_bounded_reap():
    """Adoption is opt-in per call site: a VERIFY command timing out must stay a bounded failure
    (a verify has no business continuing in the background), and the sandbox without the hook
    reaps exactly as before."""
    wd, h = _host()
    open(os.path.join(wd, "s.py"), "w", encoding="utf-8").write("import time\ntime.sleep(30)\n")
    from sliceagent.oracle import CommandOracle
    from sliceagent.execution import ToolStatus
    result = CommandOracle(f"{PY} s.py", timeout=1, root=wd).verify()
    assert result.status is ToolStatus.INDETERMINATE, result.status
    from sliceagent.sandbox import SANDBOX_TIMEOUT, LocalSandbox
    code, out = LocalSandbox(scrub_secrets=False).run(f"{PY} s.py", cwd=wd, timeout=1)
    assert code == SANDBOX_TIMEOUT and "reaped" in out, (code, out[:120])


@check
def adoption_is_gated_on_proc_tools_being_registered():
    """The default build deregisters every proc_* tool (cli.py, unless AGENT_ADVANCED_TOOLS): an
    adoption message naming proc_tail/proc_wait/proc_kill would hand the model a handle it cannot
    use, and the detached process would stay alive with nothing able to see or stop it. The hook
    must not fire, the text must not name unregistered tools, and the process takes the bounded
    reap instead."""
    wd, h = _host()
    open(os.path.join(wd, "s.py"), "w", encoding="utf-8").write("import time\ntime.sleep(30)\n")
    for name in tuple(h.registry._tools):
        if name.startswith(("proc_", "terminal_")):
            h.registry.deregister(name)
    out = h.run("run_command", {"command": f"{PY} s.py", "timeout": 1})
    text = str(out)
    assert "was NOT killed" not in text and "background as p" not in text, text[:200]
    assert "reaped" in text and "124" in text, text[:200]
    assert not re.search(r"proc_(start|wait|tail|kill)", text), text[:300]
    assert getattr(out.status, "value", out.status) == "failed", out.status
    # and with the family present (a stock host) adoption still fires and names it
    _, h2 = _host()
    open(os.path.join(h2.root(), "s.py"), "w", encoding="utf-8").write("import time\ntime.sleep(30)\n")
    out2 = h2.run("run_command", {"command": f"{PY} s.py", "timeout": 1})
    assert "was NOT killed" in str(out2)
    h2.run("proc_kill", {"handle": re.search(r"background as (p\d+)", str(out2)).group(1)})


@check
def timeout_escalation_text_is_composed_from_the_registered_tools():
    wd, h = _host()
    assert "proc_start" in h._timeout_escalation(120, False)
    for name in tuple(h.registry._tools):
        if name.startswith(("proc_", "terminal_")):
            h.registry.deregister(name)
    text = h._timeout_escalation(120, False)
    assert "proc_start" not in text and "larger timeout" in text
    assert "proc_start" not in h._timeout_escalation(600, True)


@check
def timeout_result_carries_the_escalation():
    wd, h = _host()
    open(os.path.join(wd, "slow.py"), "w", encoding="utf-8").write("import time\ntime.sleep(30)\n")
    # run_command: the deadline now DETACHES instead of killing — the escalation made automatic.
    out = h.run("run_command", {"command": f"{PY} slow.py", "timeout": 1})
    text = str(out)
    assert "was NOT killed" in text and "background as p" in text, (
        f"run_command: a deadline must detach the live process, not kill it — {text[:200]!r}")
    assert "reaped" not in text and "Exit code 124" not in text
    assert getattr(out.status, "value", out.status) == "succeeded", (
        f"an adopted deadline is not a failure verdict: {out.status!r}")
    h.run("proc_kill", {"handle": re.search(r"background as (p\d+)", text).group(1)})
    # execute_code (a batch of edits that must not keep writing unseen) keeps the bounded failure
    # plus the escalation text: re-run bigger, or move to proc_start by hand.
    out = h.run("execute_code", {"code": f"print(run({PY!r} + ' slow.py', timeout=1))"})
    assert "124" in out, f"execute_code: lost the timeout sentinel — {out!r}"
    assert "proc_start" in out, f"execute_code: no escalation to the unbounded runner — {out!r}"
    # A deadline reap is a deliberate, bounded stop with a known cause: FAILED, so the model can
    # act on the escalation (larger timeout / proc_start). INDETERMINATE parked the whole turn
    # here and stranded that advice — the park is reserved for genuinely unknown outcomes.
    assert getattr(out.status, "value", out.status) == "failed", (
        f"execute_code: a deadline reap is a bounded failure, not an unknown effect — {out.status!r}")


@check
def execute_code_honours_a_raised_deadline():
    """The regression that motivated this: execute_code is the MULTI-EDIT tool, so a hard 30s cap can
    land between two edits with no way for the caller to widen it."""
    wd, h = _host()
    code = "import time\ntime.sleep(2)\nwrite_file('done.txt', 'ok')\nprint('finished')"
    assert "finished" in h.run("execute_code", {"code": code, "timeout": 60})
    assert open(os.path.join(wd, "done.txt"), encoding="utf-8").read() == "ok"
    # …and the ceiling still holds: no caller can ask for an unbounded blocking call.
    assert h._call_timeout(10**9) == 600.0 and h._call_timeout("nonsense") == float(h.timeout)


@check
def proc_wait_converts_the_turns_cancel_token_and_spares_the_watched_process():
    """HIGH 5 (the unconverted sibling): Ctrl-C during proc_wait in the live UI froze on
    'Interrupt requested' for the full wait (measured >75s, ceiling 600s) — the exact pre-U2b
    defect at this site. The owning turn's token (cancel_scope, bound by the scheduler wave) now
    converts to the same KeyboardInterrupt the plain path's physical Ctrl-C raises — and the
    WATCH is interrupted, never the watched: a deliberately-backgrounded process survives an
    interrupted wait (proc_kill owns killing), matching the plain path exactly."""
    import threading
    from sliceagent import cancel_scope
    wd, h = _host()
    h.run("proc_start", {"command": "sleep 60"})
    fired = threading.Event()
    threading.Timer(0.3, fired.set).start()
    prev = cancel_scope.bind_cancel(fired.is_set)
    start = time.monotonic()
    try:
        h.run("proc_wait", {"handle": "p1", "timeout": 60})
        raise AssertionError("proc_wait did not convert the cancel token")
    except KeyboardInterrupt:
        pass
    finally:
        cancel_scope.unbind_cancel(prev)
    elapsed = time.monotonic() - start
    assert elapsed < 5, f"Ctrl-C held proc_wait for {elapsed:.1f}s — the wait still can't see the token"
    try:
        assert "running" in h.run("proc_poll", {"handle": "p1"}), \
            "interrupting the WATCH must not kill the watched process"
    finally:
        h.run("proc_kill", {"handle": "p1"})


@check
def proc_wait_reports_byte_evidence_liveness():
    """U7 reaches this sibling too: while proc_wait blocks, the status line shows the watched
    process's log growing (~1/s beats) — a frozen counter names a stall instead of looking like
    a crash (the review's Family H at the proc_wait site)."""
    wd, h = _host()
    open(os.path.join(wd, "chatter.py"), "w", encoding="utf-8").write(
        "import time\nprint('x' * 2000, flush=True)\ntime.sleep(2)\n")
    h.run("proc_start", {"command": f"{PY} chatter.py"})
    beats = []
    h._verify_notify = beats.append
    try:
        h.run("proc_wait", {"handle": "p1", "timeout": 5})
    finally:
        h._verify_notify = None
    assert any("proc_wait" in str(beat) and "KB output" in str(beat) for beat in beats), beats


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    sys.exit(1 if failed else 0)




@check
def sigterm_runs_the_same_cleanup_as_atexit_then_exits_128_plus_signal():
    """FIELD (the review's Family I): the CLI installed no SIGTERM handler, so every abnormal exit
    orphaned every background process — and the only escape from a wedged turn IS a signal, so this
    leak fires on the common path. The handler runs the host's atexit cleanup (procs killed, logs
    removed), then exits 128+signum (Kimi Code's 130/143 shape)."""
    if os.name != "posix":
        return
    import subprocess
    import sys as _sys
    wd, _ = _host()
    pidfile = os.path.join(wd, "child.pid")
    script = (
        "import sys, time, os; sys.path.insert(0, 'src')\n"
        "from sliceagent.tools import LocalToolHost\n"
        "h = LocalToolHost()\n"
        "handle = h.procs.start('sleep 60', cwd='.')\n"
        "proc = h.procs._procs[handle]\n"
        f"open({pidfile!r}, 'w').write(str(proc.popen.pid))\n"
        "print('READY', flush=True)\n"
        "time.sleep(30)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(os.path.dirname(__file__), "..", "src")
    p = subprocess.Popen([_sys.executable, "-c", script], cwd=wd, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert p.stdout.readline().strip() == "READY", "child never started"
        child_pid = int(open(pidfile, encoding="utf-8").read())
        os.kill(child_pid, 0)  # windows-footgun: ok -- function is POSIX-gated
        p.send_signal(15)                        # SIGTERM
        rc = p.wait(timeout=15)
        assert rc == 143, f"expected 128+SIGTERM, got {rc}"
        try:
            os.kill(child_pid, 0)  # windows-footgun: ok -- function is POSIX-gated
            alive = True
        except OSError:
            alive = False
        assert not alive, "SIGTERM left the background proc orphaned"
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


if __name__ == "__main__":
    main()
