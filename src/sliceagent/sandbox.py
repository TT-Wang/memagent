"""Sandbox — the command-execution backend.

`BaseSandbox` owns the cross-cutting concern (output capping); each backend implements only
`_exec()`. So swapping the isolation level never touches the ToolHost or the loop. Ships
`LocalSandbox` (subprocess) and `DockerSandbox` (container) behind the same seam; gVisor /
Firecracker / a remote runtime are further drop-ins.

Secret scrubbing matters: run_command executes model-proposed shell, often against
untrusted/generated code. By default the child does NOT inherit API keys or proxy creds, so
a stray `env`/exfil can't read them (Local scrubs its subprocess env; Docker only passes
explicitly-configured env into the container).

`python_cmd` lets code-as-action stay backend-portable: Local runs the venv interpreter
(so workspace code can import installed packages); Docker runs the container's `python3`.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import uuid
from typing import Protocol, runtime_checkable

from .platform_compat import (IS_WINDOWS, SIG_KILL, kill_tree,
                              popen_group_kwargs, sh as _sh)

# env var names whose values are secrets the child shouldn't see by default
_SECRET_RE = re.compile(
    r"(API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|ACCESS_KEY|PRIVATE_KEY|"
    r"(?<!NO)_PROXY$|^HTTPS?_PROXY$|^ALL_PROXY$)",
    re.IGNORECASE,
)

_OUTPUT_CAP = 1_000_000  # chars; head+tail kept, middle elided. Sized ABOVE realistic logs/diffs so the
#                          page-out blob (the recall-on-demand promise) captures the FULL output for normal
#                          large results; this is only the last-resort OOM/disk ceiling for pathological dumps.

# Internal sentinel distinct from a command that legitimately exits 124. ToolHost projects this as a
# bounded FAILURE with the escalation text (a deadline reap is deliberate and known) — INDETERMINATE
# is reserved for genuinely unknown outcomes.
SANDBOX_TIMEOUT = -124
# The deadline fired but the process was ADOPTED into the background process registry instead of
# being reaped — its work continues under a proc handle. Never a failure verdict.
SANDBOX_ADOPTED = -125


@runtime_checkable
class Sandbox(Protocol):
    """Execute a shell command, return (exit_code, combined_output)."""
    python_cmd: str
    def run(self, command: str, *, cwd: str, timeout: float) -> tuple[int, str]: ...


def _scrub_env() -> dict:
    return {k: v for k, v in os.environ.items() if not _SECRET_RE.search(k)}


def _cap(out: str) -> str:
    if len(out) <= _OUTPUT_CAP:
        return out
    keep = _OUTPUT_CAP // 2
    return out[:keep] + f"\n…[{len(out) - _OUTPUT_CAP} chars elided]…\n" + out[-keep:]


class BaseSandbox:
    """Template: run() caps output; subclasses implement _exec(). `python_cmd` is how
    code-as-action invokes Python in this backend."""
    python_cmd: str = "python3"

    def __init__(self, *, scrub_secrets: bool = True):
        self.scrub_secrets = scrub_secrets
        # Optional polled cancel (Hermes shape): a callable returning True when the owning turn was
        # interrupted. run_turn binds the turn's signal event here so a blocking wait can be
        # aborted from the LIVE UI, where no real SIGINT reaches the turn's worker thread.
        self.cancel_poll = None
        # Optional liveness heartbeat: called with the command's current output byte count (~1/s)
        # while the wait runs — the status line then shows EVIDENCE of progress (bytes growing =
        # alive; frozen = stalled) instead of a bare spinner (the review's Family H).
        self.activity_cb = None

    def run(self, command: str, *, cwd: str, timeout: float, on_timeout=None) -> tuple[int, str]:
        code, out = self._exec(command, cwd=cwd, timeout=timeout, on_timeout=on_timeout)
        return code, _cap(out)

    def _exec(self, command: str, *, cwd: str, timeout: float, on_timeout=None) -> tuple[int, str]:
        raise NotImplementedError


class LocalSandbox(BaseSandbox):
    """Local subprocess backend. cwd-confined, timeout, secret-env scrubbed. Runs the
    current (venv) interpreter for code-as-action so workspace imports resolve."""
    python_cmd = sys.executable

    @staticmethod
    def _stop_and_reap(process) -> None:
        """Best-effort process-group teardown used by both deadlines and interactive Ctrl-C."""
        kill_tree(process, signal.SIGTERM)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            kill_tree(process, SIG_KILL)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _read_log_fh(log_fh) -> str:
        log_fh.flush()
        log_fh.seek(0)
        return log_fh.read()

    def _wait_or_cancel(self, process, timeout: float, size_probe=None) -> None:
        """Bounded wait that also watches the turn's cancel token.

        process.wait(timeout=…) is a single uninterruptible syscall: in the LIVE UI (the turn runs
        on a worker thread, so no real SIGINT can reach it) a Ctrl-C only sets a cooperative Event
        that nothing inside this wait could see — the user was held for the command's full remaining
        runtime. The poll loop (Hermes' base.py pattern) re-checks the token every 50 ms and
        converts it into the same KeyboardInterrupt a physical Ctrl-C raises on the plain path, so
        the ONE existing reaper serves both frontends. ``size_probe`` (optional) returns the
        command's current output byte count and is reported to activity_cb ~1/s — liveness as
        EVIDENCE, not a spinner.
        """
        import time as _time
        deadline = _time.monotonic() + max(0.0, timeout)
        next_beat = _time.monotonic() + 1.0
        while True:
            rc = process.poll()
            if rc is not None:
                return
            poll = self.cancel_poll
            if poll is not None:
                try:
                    cancelled = poll()
                except Exception:  # noqa: BLE001 — a broken token is not a reason to kill the child
                    cancelled = False
                if cancelled:
                    raise KeyboardInterrupt
            if size_probe is not None and self.activity_cb is not None \
                    and _time.monotonic() >= next_beat:
                next_beat = _time.monotonic() + 1.0
                try:
                    self.activity_cb(size_probe())
                except Exception:  # noqa: BLE001 — liveness must never affect the command
                    pass
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            _time.sleep(min(0.05, remaining))

    def _exec(self, command: str, *, cwd: str, timeout: float, on_timeout=None) -> tuple[int, str]:
        env = _scrub_env() if self.scrub_secrets else None
        process = None
        adopted_ok = False
        # Output goes to a temp FILE from the start (never pipes): the child can never block on a
        # full pipe, wait() alone bounds the call, and a timeout can hand the SAME log — complete and
        # in order — to the background registry instead of a mid-thought pipe remnant.
        log_fd, log_path = tempfile.mkstemp(prefix=".sliceagent-run-", suffix=".log")
        log_fh = os.fdopen(log_fd, "w+", encoding="utf-8", errors="replace")
        try:
            process = subprocess.Popen(
                **_sh(command), **popen_group_kwargs(), cwd=cwd, env=env,
                # One-shot runs have NO stdin by contract (terminal.py: "Sandbox.run is one-shot and
                # has no stdin"; procman already launches with DEVNULL; interactive work belongs to
                # terminal_open's pty). Inheriting the parent's real TTY let a prompting child (npm,
                # git credential helpers, ssh, sudo) hang invisibly at 0% CPU until the deadline —
                # indistinguishable from a slow command, and fighting the TUI for the terminal.
                # DEVNULL makes the prompt fail fast and readably instead.
                stdin=subprocess.DEVNULL,
                stdout=log_fh, stderr=subprocess.STDOUT, text=True,
            )
            self._wait_or_cancel(process, timeout,
                                 size_probe=lambda: os.path.getsize(log_path))
            return process.returncode, self._read_log_fh(log_fh)
        except subprocess.TimeoutExpired:
            if on_timeout is not None:
                # The deadline does not have to be terminal (Kimi Code's autoBackgroundOnTimeout):
                # offer the LIVE process AND its complete, in-order log to the adopter (the
                # background registry). Only an adoption failure falls through to the ordinary reap.
                adopted = on_timeout(process, log_path, log_fh)
                if adopted is not None:
                    adopted_ok = True
                    return SANDBOX_ADOPTED, str(adopted)
            # Own and reap the shell's process group. This stops ordinary background descendants; the typed
            # result remains conservative because a command can deliberately escape into another session.
            self._stop_and_reap(process)
            partial = self._read_log_fh(log_fh).strip()
            suffix = f"\n{partial}" if partial else ""
            return SANDBOX_TIMEOUT, f"Command timed out after {timeout:g}s; process tree was reaped{suffix}"
        except KeyboardInterrupt:
            # Popen may be interrupted before it returns a handle. Once it has returned, however, SliceAgent
            # owns the whole process group and must not leave it mutating after the turn is sealed.
            if process is not None:
                self._stop_and_reap(process)
            raise
        except OSError as e:
            return 127, f"Could not run command: {e}"
        finally:
            # On adoption the log's ownership moved to the registry; everywhere else it dies here.
            if not adopted_ok:
                try:
                    log_fh.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    os.unlink(log_path)
                except OSError:
                    pass


class DockerSandbox(BaseSandbox):
    """Container backend: run each command in `docker run --rm`, with the workspace bind-
    mounted at the SAME path (so workspace-relative and -absolute paths match host↔container)
    and the network off by default. Only explicitly-configured env enters the container."""
    python_cmd = "python3"

    def __init__(self, image: str, *, network: str = "none", docker: str = "docker",
                 env: dict | None = None, scrub_secrets: bool = True):
        if IS_WINDOWS:
            # The Linux-image backend intentionally bind-mounts the workspace at the SAME absolute path.
            # A native ``C:\\...`` host path cannot also be the Linux container's ``-w`` path.  Reject the
            # configuration instead of starting a container whose workspace semantics are silently wrong.
            # WSL2 reports a POSIX platform and remains supported.
            raise ValueError(
                "docker sandbox is unavailable on native Windows; use AGENT_SANDBOX=local "
                "or run SliceAgent inside WSL2/Linux"
            )
        super().__init__(scrub_secrets=scrub_secrets)
        self.image = image
        # fail CLOSED: blank/whitespace network → "none" (no networking), not "drop the flag" (which gives
        # the container default bridge networking — an isolation hole).
        self.network = (network or "none").strip() or "none"
        self.docker = docker
        self.env = env or {}

    def docker_args(self, command: str, *, cwd: str, name: str | None = None) -> list[str]:
        args = [self.docker, "run", "--rm", "-v", f"{cwd}:{cwd}", "-w", cwd]
        if name:
            args += ["--name", name]
        if self.network:
            args += ["--network", self.network]
        for k, v in self.env.items():
            args += ["-e", f"{k}={v}"]
        args += [self.image, "sh", "-c", command]
        return args

    def _exec(self, command: str, *, cwd: str, timeout: float, on_timeout=None) -> tuple[int, str]:
        # on_timeout (adoption into the host proc registry) is a local-subprocess concept; a
        # container child cannot join it meaningfully, so the docker backend keeps the plain
        # named-kill timeout path.
        # Name the container so a timeout can reap it: subprocess.run only SIGKILLs the local `docker run`
        # CLI; the daemon-side container keeps running. With a name we can `docker kill` it (and --rm then
        # removes it), instead of leaking an orphan container per timeout.
        name = f"sliceagent-{uuid.uuid4().hex[:12]}"
        try:
            r = subprocess.run(self.docker_args(command, cwd=cwd, name=name),
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                subprocess.run([self.docker, "kill", name], capture_output=True, timeout=10)
            except Exception:  # noqa: BLE001 — best-effort reap; never mask the timeout result
                pass
            return SANDBOX_TIMEOUT, f"Command timed out after {timeout:g}s; container stop was requested"
        except KeyboardInterrupt:
            # Interrupting the local docker CLI does not prove the daemon-side container stopped.
            try:
                subprocess.run([self.docker, "kill", name], capture_output=True, timeout=10)
            except Exception:  # noqa: BLE001 — preserve Ctrl-C while still making a bounded cleanup attempt
                pass
            raise
        except OSError as e:
            return 127, f"Could not run docker: {e}"
        return r.returncode, (r.stdout or "") + (r.stderr or "")


def make_sandbox(backend: str = "local", *, image: str = "python:3.12-slim",
                 network: str = "none", scrub_secrets: bool = True) -> BaseSandbox:
    """Factory: 'local' (default) or 'docker'."""
    b = (backend or "local").lower()
    if b == "docker":
        return DockerSandbox(image, network=network, scrub_secrets=scrub_secrets)
    if b == "local":
        return LocalSandbox(scrub_secrets=scrub_secrets)
    # #27: a typo'd backend (e.g. "dokcer") must NOT silently fall back to the unisolated host — fail loud.
    raise ValueError(f"unknown sandbox backend {backend!r} (expected 'local' or 'docker')")
