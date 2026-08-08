"""evals/tbench/agent.py — sliceagent as a Terminal-Bench agent (REAL TB harness).

Integration shape: a HOST-SIDE BaseAgent. sliceagent's loop runs on the host (so its LLM client uses the
host's API key + proxy, which already work), and ONLY tool ACTIONS reach the task container — via a
ContainerToolHost that docker-exec's into `session.container`. Nothing is installed inside the container.

ContainerToolHost subclasses the real LocalToolHost and overrides ONLY the I/O primitives (root/_resolve/
read_text/_atomic_write/_mkparent/read+list+append handlers + execute_code + the sandbox) to act in the
container. Everything else — the tool schemas, str_replace/edit logic, the registry, the ToolText success
flag — is the unmodified product code, so we are testing the real agent, not a reimplementation.

Run (from repo root):
  LLM_API_KEY=$OPENAI_API_KEY AGENT_MODEL=gpt-5.5 HTTPS_PROXY=... PYTHONPATH=src:. \
    tb run --dataset terminal-bench-core --task-id <id> \
      --agent-import-path evals.tbench.agent:SliceagentTBAgent --n-concurrent 1
"""
from __future__ import annotations

import base64
import posixpath
import shlex
import subprocess
from pathlib import Path

from terminal_bench.agents.base_agent import AgentResult, BaseAgent
from terminal_bench.agents.failure_mode import FailureMode
from terminal_bench.terminal.tmux_session import TmuxSession

# sliceagent product code (PYTHONPATH=src)
from sliceagent.procman import ProcManager                     # noqa: E402  (container variant below)
from sliceagent.registry import ToolText                       # noqa: E402
from sliceagent.sandbox import _cap                            # noqa: E402  (head+tail output elision, prod parity)
from sliceagent.terminal import SessionManager                 # noqa: E402  (container variant below)
from sliceagent.tools import _CODE_PRELUDE, TOOL_SCHEMAS, LocalToolHost, ToolEntry  # noqa: E402


# =================================================================================================
# Container exec backend
# =================================================================================================
class ContainerSandbox:
    """A sliceagent 'sandbox' whose run() executes in the task container via docker exec. Matches the
    LocalSandbox surface the tool handlers rely on: .run(cmd, cwd, timeout) -> (exit_code, text),
    plus .python_cmd and .scrub_secrets attributes."""

    scrub_secrets = False
    python_cmd = "python3"

    def __init__(self, container, user: str = "", default_timeout: int = 60):
        self.container = container
        self.user = user or ""   # act as the task's CONFIGURED user (matches the tmux agent session), so
        self.default_timeout = default_timeout  # files sliceagent writes get the right owner for the grader
        code, _ = self._raw("command -v timeout >/dev/null 2>&1")
        self._has_timeout = code == 0

    def _raw(self, script: str):
        res = self.container.exec_run(["bash", "-lc", script], demux=False, tty=False, user=self.user)
        code = res.exit_code if hasattr(res, "exit_code") else res[0]
        out = res.output if hasattr(res, "output") else res[1]
        text = out.decode("utf-8", errors="replace") if isinstance(out, (bytes, bytearray)) else str(out or "")
        return code, text

    def run(self, command: str, cwd: str | None = None, timeout: float | None = None):
        t = int(timeout or self.default_timeout)
        inner = (f"cd {shlex.quote(cwd)} && " if cwd else "") + command
        if self._has_timeout:
            script = f"timeout {t}s bash -lc {shlex.quote(inner)}"
        else:
            script = f"bash -lc {shlex.quote(inner)}"
        # exec_run has NO timeout of its own: a backgrounded child that inherits the exec pipe can outlive
        # the inner `timeout` wrapper and block exec_run forever (same hang class as the LLM watchdog).
        # Bound it with a wall-clock watchdog; abandon a wedged exec rather than hang the whole turn.
        import concurrent.futures as _f
        ex = _f.ThreadPoolExecutor(max_workers=1, thread_name_prefix="exec-watchdog")
        fut = ex.submit(self._raw, script)
        try:
            code, text = fut.result(timeout=t + 20)
            return code, _cap(text)   # head+tail elision (production parity): a single huge output can't
            #                           bloat the in-turn transcript or push the window toward overflow
        except _f.TimeoutError:
            return 124, f"(command exceeded {t}s wall-clock and was abandoned by the harness watchdog)"
        finally:
            ex.shutdown(wait=False)


# =================================================================================================
# Container-aware live-process managers — relaunch through `docker exec` so terminal_*/proc_* act
# INSIDE the task container (not the host). This is what removes the interactive-PTY and persistent-
# background-service harness limits: terminal_open drives a REAL TTY in the container (REPLs, expect/
# telnet, TUIs); proc_start backgrounds a container process whose output streams to a host logfile.
# =================================================================================================
def _docker_argv(container, cwd: str, *, tty: bool, command: str | None):
    argv = ["docker", "exec", "-i"] + (["-t"] if tty else []) + ["-w", cwd or "/app", container.id]
    return argv + (["bash", "-lc", command] if command else ["bash"])


class ContainerSessionManager(SessionManager):
    """PTY session whose process is `docker exec -it` INTO the task container — so terminal_open/send/
    wait drive a real interactive terminal inside it (the `-t` allocates a container-side TTY; the host
    slave PTY carries the bytes). close() kills the host exec client, which tears down the container exec."""

    def __init__(self, container, *, scrub_secrets: bool = False):
        super().__init__(scrub_secrets=scrub_secrets)
        self.container = container

    def _spawn_pty(self, command, cwd, env, slave):
        return subprocess.Popen(_docker_argv(self.container, cwd, tty=True, command=command),
                                env=env, stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True, close_fds=True)


class ContainerProcManager(ProcManager):
    """Background process run via `docker exec` INSIDE the task container (servers, long builds). Output
    streams to a HOST logfile that poll/tail/wait read; kill ends the host exec client → the daemon stops.
    (A service that must outlive the agent for the grader should instead be left running via run_command
    `nohup setsid … &`, which survives in the container; proc_* is for launch-probe within the run.)"""

    def __init__(self, container, *, scrub_secrets: bool = False):
        super().__init__(scrub_secrets=scrub_secrets)
        self.container = container

    def _spawn_proc(self, command, cwd, env, log_fh):
        return subprocess.Popen(_docker_argv(self.container, cwd, tty=False, command=command),
                                env=env, stdin=subprocess.DEVNULL, stdout=log_fh,
                                stderr=subprocess.STDOUT, start_new_session=True)


# =================================================================================================
# ToolHost that acts inside the container
# =================================================================================================
class ContainerToolHost(LocalToolHost):
    """LocalToolHost whose file + shell I/O target the task container. Reuses the parent's schemas,
    registry, str_replace/edit_file logic (they go through read_text/_atomic_write/_resolve, all
    overridden here) and the ToolText success flag — only the bytes move to/from the container."""

    # container builtins. proc_*/terminal_* ARE included here — but bound to CONTAINER-aware managers
    # (set in __init__) that docker-exec into the task container, so they act in the container, not the
    # host. This removes the interactive-PTY (terminal_*) and persistent-background-service (proc_*) limits.
    _CONTAINER_TOOLS = {
        "read_file", "list_files", "edit_file", "append_to_file", "str_replace",
        "run_command", "execute_code", "ask_user", "world_set", "world_clear",
        "proc_start", "proc_poll", "proc_tail", "proc_wait", "proc_kill",
        "terminal_open", "terminal_send", "terminal_read", "terminal_wait", "terminal_close",
    }

    def __init__(self, container, workdir: str, user: str = "", timeout: int = 60):
        self.container = container
        super().__init__(root=workdir, sandbox=ContainerSandbox(container, user, timeout), timeout=timeout)
        # the parent constructed HOST managers; replace them with container-aware ones so terminal_*/proc_*
        # spawn via docker exec INTO the task container (the I/O primitives are already container-routed).
        self.procs = ContainerProcManager(container)
        self.terminals = ContainerSessionManager(container)
        self._exec_seq = 0

    def _register_builtins(self) -> None:
        handlers = {
            "read_file": self._t_read_file, "list_files": self._t_list_files,
            "edit_file": self._t_edit_file, "append_to_file": self._t_append,
            "str_replace": self._t_str_replace, "run_command": self._t_run_command,
            "execute_code": self._t_execute_code, "ask_user": self._t_ask_user,
            "world_set": self._t_world_set, "world_clear": self._t_world_clear,
            "proc_start": self._t_proc_start, "proc_poll": self._t_proc_poll,
            "proc_tail": self._t_proc_tail, "proc_wait": self._t_proc_wait,
            "proc_kill": self._t_proc_kill,
            "terminal_open": self._t_terminal_open, "terminal_send": self._t_terminal_send,
            "terminal_read": self._t_terminal_read, "terminal_wait": self._t_terminal_wait,
            "terminal_close": self._t_terminal_close,
        }
        for schema in TOOL_SCHEMAS:
            name = schema["function"]["name"]
            if name not in self._CONTAINER_TOOLS:
                continue
            self.registry.register(ToolEntry(
                name=name, schema=schema, handler=handlers[name],
                accesses=(lambda args, n=name: self._builtin_accesses(n, args)),
                source="builtin",
            ))

    # --- path handling: container paths, no host realpath / escape checks (the container is the sandbox)
    def root(self) -> str:
        return self._root or "/app"

    def add_root(self, path):
        return None

    def allowed_roots(self):
        return [self.root()]

    def _grant_shell_paths(self, text: str) -> None:
        return  # no host-reach concern inside the container

    def _resolve(self, path: str) -> str:
        if not path:
            raise ValueError("empty path")
        if path.startswith("~"):
            path = "/root" + path[1:]
        if posixpath.isabs(path):
            return posixpath.normpath(path)
        return posixpath.normpath(self.root().rstrip("/") + "/" + path)

    # --- file I/O via the container
    def _mkparent(self, full: str) -> None:
        parent = posixpath.dirname(full)
        if parent:
            self.sandbox.run(f"mkdir -p {shlex.quote(parent)}", timeout=15)

    def _make_executable(self, full: str) -> None:
        self.sandbox.run(f"chmod +x {shlex.quote(full)}", timeout=15)

    def _atomic_write(self, full: str, content: str) -> None:
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        code, out = self.sandbox.run(f"echo {b64} | base64 -d > {shlex.quote(full)}", timeout=60)
        if code != 0:
            raise OSError(out.strip() or f"write failed: {full}")

    def read_text(self, path: str) -> str:
        full = self._resolve(path)
        code, out = self.sandbox.run(f"cat -- {shlex.quote(full)}", timeout=30)
        if code != 0:
            raise FileNotFoundError(out.strip() or full)
        if "\x00" in out[:8192]:
            raise ValueError(f"{path} appears to be binary; not shown")
        return out

    def _t_read_file(self, args: dict):
        full = self._resolve(args["path"])
        code, out = self.sandbox.run(f"cat -- {shlex.quote(full)}", timeout=30)
        if code != 0:
            return ToolText(f"Error: cannot read {args['path']}: {out.strip()}", ok=False)
        if "\x00" in out[:8192]:
            h_code, h = self.sandbox.run(f"xxd -l 256 -- {shlex.quote(full)} 2>/dev/null || "
                                         f"od -A x -t x1z -N 256 -- {shlex.quote(full)}", timeout=15)
            return (f"{args['path']}: binary file — text tools can't edit it; inspect/convert it with "
                    f"run_command/execute_code.\nhexdump (first 256 bytes):\n{h}")
        return out

    def _t_list_files(self, args: dict):
        base = self._resolve(args.get("path") or ".")
        if args.get("recursive"):
            cmd = (f"find {shlex.quote(base)} -type f -not -path '*/.git/*' "
                   f"-not -path '*/__pycache__/*' | head -500")
        else:
            cmd = f"ls -1Ap {shlex.quote(base)}"
        code, out = self.sandbox.run(cmd, timeout=30)
        if code != 0:
            return ToolText(f"Error: cannot list {args.get('path') or '.'}: {out.strip()}", ok=False)
        return out.strip() or "(empty)"

    def _t_append(self, args: dict):
        full = self._resolve(args["path"])
        self._mkparent(full)
        b64 = base64.b64encode(args["content"].encode("utf-8")).decode("ascii")
        code, out = self.sandbox.run(f"echo {b64} | base64 -d >> {shlex.quote(full)}", timeout=60)
        if code != 0:
            return ToolText(f"Error: append failed: {out.strip()}", ok=False)
        return f"Appended {len(args['content'])} bytes to {args['path']}"

    def _t_execute_code(self, args: dict):
        # PREPEND the same helper prelude the core/host injects (read_file/write_file/run/str_replace/...).
        # Without it, the in-sandbox helpers the tool ADVERTISES are missing in the container and the
        # model's code dies with NameError (observed on polyglot/org-json). Restores the documented API.
        script = _CODE_PRELUDE + (args.get("code") or args.get("script") or "")
        self._exec_seq += 1
        path = f"/tmp/.sliceagent-exec-{self._exec_seq}.py"
        self._atomic_write(path, script)
        code, out = self.sandbox.run(f"python3 {shlex.quote(path)}", cwd=self.root(), timeout=self.timeout)
        self.sandbox.run(f"rm -f {shlex.quote(path)}", timeout=10)
        out = out.strip()
        if code != 0:
            return ToolText(f"Exit code {code}\n{out or '(no output)'}", ok=False)
        return out or "(execute_code produced no output)"


# =================================================================================================
# Null retriever (no host-side code index over a container FS)
# =================================================================================================
class _NullRetriever:
    def retrieve(self, query, k: int = 6):
        return []


# =================================================================================================
# The agent
# =================================================================================================
class SliceagentTBAgent(BaseAgent):
    @staticmethod
    def name() -> str:
        return "sliceagent"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # accepts (and ignores) tb's --model: sliceagent's LLM is configured from env (AGENT_MODEL/LLM_API_KEY)
        self._max_steps = int(kwargs.get("max_steps", 60))

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
    ) -> AgentResult:
        from sliceagent.events import make_dispatcher
        from sliceagent.hooks import BudgetHook, CatastrophicSafeguardHook, CompositeHooks
        from sliceagent.llm import OpenAILLM
        from sliceagent.loop import run_turn
        from sliceagent.memory import make_memory
        from sliceagent.session import Session
        from sliceagent.pfc import record_user, slice_sink
        from sliceagent.seed import make_build_slice

        container = session.container
        user = getattr(session, "_user", "") or ""   # act as the same user TB's agent session uses
        code, wd = self._exec(container, "pwd", user)
        workdir = (wd.strip() or "/app") if code == 0 else "/app"

        tools = ContainerToolHost(container, workdir, user=user)
        llm = OpenAILLM()
        sess = Session(make_memory())
        try:
            llm.set_cache_key(sess.session_id)
        except Exception:
            pass
        sess.new_topic(instruction)
        record_user(sess.active(), instruction)

        build = make_build_slice(sess, tools, _NullRetriever(), make_memory(), instruction, sess.session_id)

        # action log → logging_dir/sliceagent_actions.log so a hang/failure is diagnosable (the tmux pane is
        # empty because sliceagent acts via docker exec, not the recorded shell).
        log_f = None
        if logging_dir is not None:
            try:
                logging_dir.mkdir(parents=True, exist_ok=True)
                log_f = open(logging_dir / "sliceagent_actions.log", "a", encoding="utf-8")
            except Exception:
                log_f = None

        def _log(e):
            if log_f is None:
                return
            try:
                t = type(e).__name__
                if t in ("ToolStarted", "ToolCall"):
                    log_f.write(f"TOOL {getattr(e, 'name', '?')} {str(getattr(e, 'args', ''))[:200]}\n")
                elif t == "ToolResult":
                    fail = "FAIL " if getattr(e, "failing", False) else ""
                    log_f.write(f"  -> {fail}{(getattr(e, 'output', '') or '')[:300]}\n")
                elif t == "AssistantText":
                    log_f.write(f"SAY {(getattr(e, 'content', '') or '')[:300]}\n")
                elif t in ("TurnEnd", "TurnInterrupted"):
                    why = getattr(e, "stop_reason", getattr(e, "reason", "?"))
                    msg = getattr(e, "message", "") or ""
                    log_f.write(f"[{t} {why}{(' :: ' + msg[:200]) if msg else ''}]\n")
                log_f.flush()
            except Exception:
                pass

        dispatch = make_dispatcher(slice_sink(sess), _log)
        hooks = CompositeHooks(
            CatastrophicSafeguardHook(),
            BudgetHook(4_000_000),
        )
        try:
            result = run_turn(build_slice=build, llm=llm, tools=tools, dispatch=dispatch,
                              hooks=hooks, max_steps=self._max_steps)
            usage = result.usage or {}
            return AgentResult(
                total_input_tokens=int(usage.get("prompt_tokens", 0)),
                total_output_tokens=int(usage.get("completion_tokens", 0)),
                failure_mode=FailureMode.NONE,
            )
        except Exception:
            return AgentResult(failure_mode=FailureMode.UNKNOWN_AGENT_ERROR)

    @staticmethod
    def _exec(container, script: str, user: str = ""):
        res = container.exec_run(["bash", "-lc", script], demux=False, tty=False, user=user)
        code = res.exit_code if hasattr(res, "exit_code") else res[0]
        out = res.output if hasattr(res, "output") else res[1]
        text = out.decode("utf-8", errors="replace") if isinstance(out, (bytes, bytearray)) else str(out or "")
        return code, text
