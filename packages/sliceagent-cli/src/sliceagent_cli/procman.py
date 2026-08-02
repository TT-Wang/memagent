"""procman — background / long-running processes for the agent (the gap the one-shot
``Sandbox.run`` can't fill).

``Sandbox.run`` blocks and returns only on exit, so two whole classes of work are
inexpressible: (1) "start a server, then probe it" (the server never exits), and (2)
multi-minute builds that overrun the run timeout and come back as exit 124. ``ProcManager``
keeps live children in a registry keyed by a short handle (``p1``, ``p2``, …) so the agent
can start a process, keep it alive across turns, ``poll`` / ``tail`` / ``wait``, then ``kill``.

Local subprocess backend (the eval path); cwd-confined and secret-env-scrubbed exactly like
``LocalSandbox``. Output streams to a temp LOGFILE (not a pipe) so ``tail``/``wait`` can read it
AFTER the call returns — a ``Popen`` pipe would deadlock once its OS buffer fills. Children run
in their own process group (``start_new_session=True``) so ``kill`` takes down the whole tree
(a server that forks workers, a build that spawns sub-makes). ``PYTHONUNBUFFERED`` is forced so
Python children flush to the logfile promptly instead of after exit.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

from sliceagent_core import cancel_scope
from sliceagent_core.platform_compat import (ProcessGroupTerminationError, capture_pgid,
                              popen_group_kwargs, process_group_alive, sh as _sh,
                              terminate_process_group)

from .sandbox import _scrub_env

_TAIL_CHARS = 4000  # cap a tail read so a chatty process can't flood the slice


class _Proc:
    __slots__ = ("handle", "cmd", "popen", "log_path", "log_fh", "pgid", "group_extinct")

    def __init__(self, handle: str, cmd: str, popen, log_path: str, log_fh, pgid=None):
        self.handle = handle
        self.cmd = cmd
        self.popen = popen
        self.log_path = log_path
        self.log_fh = log_fh
        self.pgid = pgid   # POSIX process-group id captured at spawn (== leader pid); None on Windows
        self.group_extinct = False


class ProcManager:
    """Registry of live background processes. Not threadsafe (the agent loop is single-threaded)."""

    def __init__(self, *, scrub_secrets: bool = True, term_grace: float = 3.0,
                 kill_grace: float = 2.0):
        self.scrub_secrets = scrub_secrets
        self.term_grace = max(0.0, float(term_grace))
        self.kill_grace = max(0.0, float(kill_grace))
        self._procs: dict[str, _Proc] = {}
        self._n = 0

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self, command: str, *, cwd: str) -> str:
        """Launch `command` in the background; return a handle. Non-blocking."""
        self._n += 1
        handle = f"p{self._n}"
        fd, log_path = tempfile.mkstemp(prefix=f".sliceagent-{handle}-", suffix=".log")
        log_fh = os.fdopen(fd, "wb")
        env = _scrub_env() if self.scrub_secrets else dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            popen = self._spawn_proc(command, cwd, env, log_fh)
        except BaseException:   # spawn failed (bad cwd, exec error, container-seam failure) — release the fd + temp file
            try:
                log_fh.close()
            finally:
                try:
                    os.unlink(log_path)
                except OSError:
                    pass
            raise
        # Capture the process-group id NOW, while the leader is alive: getpgid raises once the leader exits
        # and is reaped, so a later kill could no longer find the group to reach an orphaned background child
        # (external review H-14). Via the platform_compat seam (Windows-safe).
        self._procs[handle] = _Proc(handle, command, popen, log_path, log_fh, capture_pgid(popen))
        return handle

    def _spawn_proc(self, command, cwd, env, log_fh):
        """Launch the background process. OVERRIDABLE SEAM: a container variant relaunches via
        `docker exec` so the process runs INSIDE the task container, streaming to this host logfile."""
        return subprocess.Popen(
            **_sh(command), cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT,
            **popen_group_kwargs(),
        )

    def adopt(self, popen, command: str, cwd: str, log_path: str, log_fh) -> str:
        """Take over a LIVE process whose blocking caller hit its deadline; return a handle.

        The process is NOT reaped: its work so far is preserved, and because the caller's output
        already streamed to this same logfile from the start, the log is complete and in order and
        new output keeps landing with zero plumbing. Group/poll/kill semantics are identical to a
        proc_start child — the one divergence the timeout path used to make unrecoverable by
        killing the tree.
        """
        self._n += 1
        handle = f"p{self._n}"
        self._procs[handle] = _Proc(handle, command, popen, log_path, log_fh, capture_pgid(popen))
        return handle

    def poll(self, handle: str) -> str:
        p = self._get(handle)
        rc = p.popen.poll()
        if rc is not None and p.log_fh is not None:   # self-exited proc: release the write fd now (don't leak it to cleanup)
            try:
                p.log_fh.close()
            except Exception:  # noqa: BLE001
                pass
            p.log_fh = None
        if rc is None:
            return "running"
        if p.group_extinct:
            return f"exited {rc}"
        group_alive = process_group_alive(p.pgid, p.popen)
        if group_alive is False:
            p.group_extinct = True
            return f"exited {rc}"
        if group_alive is True:
            return f"leader exited {rc}; descendants running"
        return f"leader exited {rc}; descendant state unknown"

    def tail(self, handle: str, lines: int = 40) -> str:
        p = self._get(handle)
        body = self._read_log(p, lines)
        return f"[{handle} {self.poll(handle)}]\n{body}"

    def wait(self, handle: str, timeout: float) -> str:
        p = self._get(handle)
        # The same conversion the sandbox wait has (U2b): popen.wait(timeout=…) is a single
        # uninterruptible syscall — in the live UI a Ctrl-C only sets a cooperative Event that
        # nothing inside the wait could see, holding the user for the full timeout (measured
        # >75s against a 600s ceiling — the exact pre-fix defect at this sibling site). Poll the
        # owning turn's token every 50ms and convert to the same KeyboardInterrupt the plain
        # path's physical Ctrl-C raises. The process is NOT reaped: proc_wait watches a
        # deliberately-backgrounded child, so interrupting the WATCH must not kill the watched
        # (proc_kill owns that) — matching the plain path exactly.
        poll = cancel_scope.current_cancel()
        activity = cancel_scope.current_activity()
        deadline = time.monotonic() + max(0.0, timeout)
        next_beat = time.monotonic() + 1.0
        while True:
            if p.popen.poll() is not None:
                status = self.poll(handle)  # includes whole-group state, not merely the leader's rc
                break
            if poll is not None:
                try:
                    cancelled = poll()
                except Exception:  # noqa: BLE001 — a broken token is not a reason to abandon the watch
                    cancelled = False
                if cancelled:
                    raise KeyboardInterrupt
            if activity is not None and time.monotonic() >= next_beat:
                next_beat = time.monotonic() + 1.0
                try:
                    activity(os.path.getsize(p.log_path))
                except Exception:  # noqa: BLE001 — liveness must never affect the wait
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = f"running (still alive after {timeout:g}s)"
                break
            time.sleep(min(0.05, remaining))
        return f"[{handle} {status}]\n{self._read_log(p, 40)}"

    def kill(self, handle: str, *, term_grace=None, kill_grace=None) -> str:
        p = self._get(handle)
        # Termination success is about the whole spawn-captured GROUP, not the shell leader. The shared
        # primitive signals even after the leader exits, waits for group extinction, then escalates. Never
        # report "killed" merely because Popen.wait() reaped the leader while descendants survived.
        # The grace overrides bound the SIGNAL-path sweep: it runs under a supervisor's SIGKILL
        # deadline (docker stop gives ~10s), so N SIGTERM-ignoring children at the default 3s+2s
        # each would be cut mid-sweep (measured 9.22s for three — what provokes the second signal).
        extinct = terminate_process_group(
            p.pgid, p.popen,
            term_timeout=self.term_grace if term_grace is None else max(0.0, float(term_grace)),
            kill_timeout=self.kill_grace if kill_grace is None else max(0.0, float(kill_grace)),
        )
        if not extinct:
            raise ProcessGroupTerminationError(
                f"could not prove process group for {handle} is extinct after TERM/KILL; "
                "descendants may still be running"
            )
        p.group_extinct = True
        status = self.poll(handle)
        # Release the open FD now (the process is dead, so nothing more writes the log) — a long session that
        # starts/kills many procs would otherwise leak one fd per cycle, marching toward EMFILE. Keep the
        # registry entry + on-disk log so proc_poll/proc_tail still work after a kill (the confirm-after-kill
        # UX); cleanup() unlinks the temp file at session end.
        if p.log_fh is not None:
            try:
                p.log_fh.close()
            except Exception:  # noqa: BLE001
                pass
            p.log_fh = None
        return f"killed {handle} ({status})"

    def list(self) -> str:
        if not self._procs:
            return "(no background processes)"
        return "\n".join(f"{h}: {self.poll(h)} — {p.cmd}" for h, p in self._procs.items())

    def cleanup(self, *, term_grace=None, kill_grace=None) -> None:
        """Kill every live child and remove its logfile. Call at session end; never raises.
        The grace overrides bound the sweep for the signal path (a supervisor's SIGKILL deadline
        leaves no room for the default 5s per SIGTERM-ignoring child)."""
        for h in list(self._procs):
            try:
                self.kill(h, term_grace=term_grace, kill_grace=kill_grace)
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            p = self._procs.pop(h, None)
            if not p:
                continue
            try:
                p.log_fh.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                os.unlink(p.log_path)
            except OSError:
                pass

    # ── internals ──────────────────────────────────────────────────────────
    def _get(self, handle: str) -> _Proc:
        p = self._procs.get(handle)
        if p is None:
            raise ValueError(
                f"unknown process handle {handle!r}. Live: {', '.join(self._procs) or '(none)'}")
        return p

    @staticmethod
    def _read_log(p: _Proc, lines: int) -> str:
        try:
            p.log_fh.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(p.log_path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
        except OSError:
            data = ""
        tail = "\n".join(data.splitlines()[-max(1, lines):])
        if len(tail) > _TAIL_CHARS:
            tail = "…[earlier output elided]…\n" + tail[-_TAIL_CHARS:]
        return tail or "(no output yet)"
