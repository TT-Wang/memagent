"""The SIGTERM sweep's honesty and reach (the review's U8 findings 6&7).

No model, no pytest. Run: PYTHONPATH=src python tests/test_signal_sweep.py
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

CHECKS = []
def check(fn):
    CHECKS.append(fn)
    return fn


SRC = os.path.join(os.path.dirname(__file__), "..", "src")
PY = sys.executable


def _drive(script: str, wd: str):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    return subprocess.Popen([PY, "-c", script], cwd=wd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _reap_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


@check
def a_second_signal_mid_sweep_dies_BY_the_signal_not_a_fake_graceful_143():
    """FINDING 6 (re-entrancy): a second SIGTERM 0.8s into the sweep re-entered the handler, hit
    the already-set _closed guard, and os._exit(143)'d with background groups still ALIVE — a
    graceful code lying about a skipped sweep, indistinguishable upstream. The honest shape:
    death BY the signal (wait status WIFSIGNALED, rc == -SIGTERM), plainly distinct from a
    completed sweep's exit 143."""
    wd = tempfile.mkdtemp(prefix="sigsweep-")
    pidfile = os.path.join(wd, "bg.pid")
    script = (
        "import sys, time, os; sys.path.insert(0, 'src')\n"
        "from sliceagent.tools import LocalToolHost\n"
        "h = LocalToolHost()\n"
        # a background child that IGNORES SIGTERM, so the sweep is still inside its term grace
        # when the second signal lands at +0.8s
        "handle = h.procs.start(\"%s -c \\\"import signal,time; signal.signal(signal.SIGTERM,"
        " signal.SIG_IGN); time.sleep(60)\\\"\", cwd='.')\n"
        "proc = h.procs._procs[handle]\n"
        f"open({pidfile!r}, 'w').write(str(proc.popen.pid))\n"
        "print('READY', flush=True)\n"
        "time.sleep(30)\n" % PY
    )
    p = _drive(script, wd)
    bg_pid = None
    try:
        assert p.stdout.readline().strip() == "READY", "child never started"
        bg_pid = int(open(pidfile).read())
        assert _alive(bg_pid), "the SIGTERM-ignoring background child never spawned"
        time.sleep(0.6)                    # let the child actually INSTALL its SIG_IGN first —
        # signalling inside the import/install window kills it with the default disposition and
        # the sweep finishes before the second signal (the re-entrancy path would never run)
        p.send_signal(signal.SIGTERM)
        time.sleep(0.8)                    # mid-sweep: inside the bounded 1.0s term grace
        p.send_signal(signal.SIGTERM)      # the impatient second signal
        rc = p.wait(timeout=20)
        assert rc == -signal.SIGTERM, (
            f"expected death BY SIGTERM (rc {-signal.SIGTERM}), got {rc} — "
            "a fake graceful 143 would claim a completed sweep with groups still alive")
    finally:
        if bg_pid is not None:
            _reap_quietly(bg_pid)
        if p.poll() is None:
            p.kill()
            p.wait()


@check
def the_signal_sweep_is_bounded_under_a_supervisor_deadline():
    """FINDING 6 (bounded sweep): the default 3s+2s per-child grace measured 9.22s for three
    SIGTERM-ignoring children — past docker stop's 10s, mid-sweep SIGKILL territory, and the
    provocation for the second signal. The signal path now sweeps with 1.0s/0.5s graces and
    still reaps every group."""
    wd = tempfile.mkdtemp(prefix="sigsweep-")
    pidfile = os.path.join(wd, "bg.pids")
    ignoring = ("%s -c \\\"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                " time.sleep(60)\\\"" % PY)
    script = (
        "import sys, time, os; sys.path.insert(0, 'src')\n"
        "from sliceagent.tools import LocalToolHost\n"
        "h = LocalToolHost()\n"
        "pids = []\n"
        "for _ in range(3):\n"
        f"    handle = h.procs.start(\"{ignoring}\", cwd='.')\n"
        "    pids.append(str(h.procs._procs[handle].popen.pid))\n"
        f"open({pidfile!r}, 'w').write('\\n'.join(pids))\n"
        "print('READY', flush=True)\n"
        "time.sleep(30)\n"
    )
    p = _drive(script, wd)
    try:
        assert p.stdout.readline().strip() == "READY", "child never started"
        pids = [int(x) for x in open(pidfile).read().split()]
        assert len(pids) == 3 and all(_alive(pid) for pid in pids)
        time.sleep(0.6)                    # let all three children INSTALL SIG_IGN before signalling
        start = time.monotonic()
        p.send_signal(signal.SIGTERM)
        rc = p.wait(timeout=25)
        elapsed = time.monotonic() - start
        assert rc == 143, f"a COMPLETED bounded sweep must exit 128+SIGTERM, got {rc}"
        assert elapsed < 7.0, (
            f"the bounded sweep took {elapsed:.1f}s for 3 SIGTERM-ignoring children — "
            "the unbounded 3s+2s grace shape is back (measured 9.22s in the review)")
        assert not any(_alive(pid) for pid in pids), "the bounded sweep left a group alive"
    finally:
        for pid in [int(x) for x in open(pidfile).read().split()] if os.path.exists(pidfile) else []:
            _reap_quietly(pid)
        if p.poll() is None:
            p.kill()
            p.wait()


@check
def sigterm_reaps_the_in_flight_FOREGROUND_command():
    """FINDING 7 (foreground reach): the sweep never reached an in-flight run_command — its
    Popen handle lived only in a local variable inside sandbox._exec, so a SIGTERM during
    `npm run dev` / a build orphaned the whole group holding ports and locks. The sandbox now
    registers in-flight processes and the sweep reaps them first."""
    wd = tempfile.mkdtemp(prefix="sigsweep-")
    pidfile = os.path.join(wd, "fg.pid")
    script = (
        "import sys, time, os, threading; sys.path.insert(0, 'src')\n"
        "from sliceagent.tools import LocalToolHost\n"
        "h = LocalToolHost()\n"
        "def fg():\n"
        f"    h.sandbox.run(\"sh -c 'echo $$ > {pidfile}; exec sleep 60'\","
        f" cwd={wd!r}, timeout=60)\n"
        "threading.Thread(target=fg, daemon=True).start()\n"
        "print('READY', flush=True)\n"
        "time.sleep(30)\n"
    )
    p = _drive(script, wd)
    fg_pid = None
    try:
        assert p.stdout.readline().strip() == "READY", "child never started"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not os.path.exists(pidfile):
            time.sleep(0.05)
        assert os.path.exists(pidfile), "the foreground command never spawned"
        fg_pid = int(open(pidfile).read())
        assert _alive(fg_pid), "the foreground sleep never started"
        p.send_signal(signal.SIGTERM)
        rc = p.wait(timeout=20)
        assert rc == 143, f"expected a completed sweep (143), got {rc}"
        assert not _alive(fg_pid), (
            "SIGTERM orphaned the in-flight foreground command — the sweep still can't reach it")
    finally:
        if fg_pid is not None:
            _reap_quietly(fg_pid)
        if p.poll() is None:
            p.kill()
            p.wait()


if __name__ == "__main__":
    ok = 0
    for fn in CHECKS:
        try:
            fn()
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(CHECKS)} passed")
    sys.exit(0 if ok == len(CHECKS) else 1)
