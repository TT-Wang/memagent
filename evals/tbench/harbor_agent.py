"""sliceagent + codex-subscription as HARBOR (Terminal-Bench 2.0) agents.

harbor's agent API is async (`setup`/`run` over a `BaseEnvironment` whose `exec()` runs commands in the
task container). Two agents here:

  SliceagentHarborAgent       — sliceagent's loop runs on the HOST (slice built host-side = the moat), and
                              tool ACTIONS reach the container through a sync↔async bridge over env.exec.
  CodexSubscriptionHarborAgent — installs codex in the container + injects the host ~/.codex OAuth tokens
                              (ChatGPT subscription), then `codex exec`s the instruction.

Run (from repo root, with the gpt-5.5 key in env for sliceagent):
  LLM_API_KEY=$OPENAI_API_KEY AGENT_MODEL=gpt-5.5 PYTHONPATH=src:. .venv/bin/harbor run \
    --path evals/tbench/tb2 -i regex-log \
    --agent-import-path evals.tbench.harbor_agent:SliceagentHarborAgent -m gpt-5.5
  PYTHONPATH=src:. .venv/bin/harbor run --path evals/tbench/tb2 -i regex-log \
    --agent-import-path evals.tbench.harbor_agent:CodexSubscriptionHarborAgent -m gpt-5.5
"""
from __future__ import annotations

import asyncio
import base64
import os
import shlex
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from sliceagent.sandbox import _cap  # head+tail output elision (prod parity)
from sliceagent.tools import LocalToolHost  # noqa: E402

# reuse the container tool host's file-I/O overrides (read_text/_atomic_write/_resolve/_t_* via self.sandbox)
from evals.tbench.agent import ContainerToolHost, _NullRetriever


# =================================================================================================
# sync sandbox backed by harbor's async env.exec
# =================================================================================================
class _EnvSandbox:
    """The sliceagent Sandbox surface (.run(cmd,cwd,timeout)->(code,text), .python_cmd, .scrub_secrets),
    bridged onto harbor's async ``environment.exec`` via the running event loop (sliceagent's loop is sync
    and runs in a worker thread)."""

    scrub_secrets = False
    python_cmd = "python3"

    def __init__(self, env: BaseEnvironment, loop, default_timeout: int = 60):
        self._env = env
        self._loop = loop
        self.default_timeout = default_timeout

    def run(self, command: str, cwd: str | None = None, timeout: float | None = None):
        t = int(timeout or self.default_timeout)
        fut = asyncio.run_coroutine_threadsafe(
            self._env.exec(command, cwd=cwd, timeout_sec=t), self._loop)
        try:
            r = fut.result(timeout=t + 30)
        except Exception as e:  # noqa: BLE001 — a wedged exec returns as a failed tool result, never hangs the turn
            return 124, f"(exec error: {type(e).__name__}: {e})"
        out = (r.stdout or "") + (r.stderr or "")
        return r.return_code, _cap(out)


class _HarborSliceagentHost(ContainerToolHost):
    """ContainerToolHost (its file/shell/code overrides go through self.sandbox) but backed by the
    harbor env sandbox. MVP toolset: file ops + run_command + execute_code (no proc_*/terminal_* yet —
    those need a container handle; ~51/56 tasks don't need them)."""

    _CONTAINER_TOOLS = {
        "read_file", "list_files", "edit_file", "append_to_file", "str_replace",
        "run_command", "execute_code", "ask_user", "world_set", "world_clear",
    }

    def __init__(self, sandbox: _EnvSandbox, workdir: str, timeout: int = 60):
        # bypass ContainerToolHost.__init__ (needs a docker container) — init LocalToolHost directly
        LocalToolHost.__init__(self, root=workdir, sandbox=sandbox, timeout=timeout)
        self._exec_seq = 0


# =================================================================================================
# sliceagent as a harbor agent
# =================================================================================================
class SliceagentHarborAgent(BaseAgent):
    SUPPORTS_WINDOWS = False

    @staticmethod
    def name() -> str:
        return "sliceagent"

    def __init__(self, logs_dir: Path, model_name: str | None = None, task_dir: Path | None = None,
                 trial_paths=None, extra_env: dict | None = None, agent_timeout_sec: float | None = None,
                 **kwargs):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self._logs_dir = Path(logs_dir)
        # task id for the trajectory filename: prefer task_dir, else derive from the trial dir (<task>__<id>)
        self._task = (Path(task_dir).name if task_dir else None) or next(
            (p.split("__")[0] for p in [Path(logs_dir).name, *(a.name for a in Path(logs_dir).parents)]
             if "__" in p), "task")
        self._extra_env = extra_env or {}
        self._timeout = int(agent_timeout_sec or 60)
        self._max_steps = int(self._extra_env.get("AGENT_MAX_STEPS", "60"))

    def version(self) -> str:
        return "1.0.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        return

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        loop = asyncio.get_running_loop()
        try:
            wd = await environment.exec("pwd")
            workdir = (wd.stdout or "").strip() or "/app"
        except Exception:  # noqa: BLE001
            workdir = "/app"
        sandbox = _EnvSandbox(environment, loop, default_timeout=self._timeout)
        # HARD wall-clock bound: harbor's per-task timeout can't cancel a host-thread, so if sliceagent's
        # loop hangs (e.g. a wedged env.exec) it would stall the whole round. wait_for returns control to
        # harbor (which records a timeout and moves on); the orphan thread dies when its env.exec fails.
        try:
            out = await asyncio.wait_for(
                asyncio.to_thread(self._run_sliceagent, instruction, sandbox, workdir),
                timeout=self._timeout + 120)
        except (TimeoutError, asyncio.TimeoutError):
            out = {"usage": {}, "steps": None}
        usage = out.get("usage") or {}
        steps = out.get("steps")
        context.n_input_tokens = usage.get("prompt_tokens")
        context.n_output_tokens = usage.get("completion_tokens")
        context.metadata = {"steps": steps}
        try:  # robust metrics sidecar (compare.py prefers result.json, falls back to this)
            import json as _j
            self._logs_dir.mkdir(parents=True, exist_ok=True)
            (self._logs_dir / "metrics.json").write_text(_j.dumps({
                "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
                "steps": steps}))
        except Exception:  # noqa: BLE001
            pass

    def _run_sliceagent(self, instruction: str, sandbox: _EnvSandbox, workdir: str) -> dict:
        from sliceagent.events import make_dispatcher
        from sliceagent.hooks import BudgetHook, CatastrophicSafeguardHook, CompositeHooks
        from sliceagent.llm import OpenAILLM
        from sliceagent.loop import run_turn
        from sliceagent.memory import make_memory
        from sliceagent.session import Session
        from sliceagent.pfc import record_user, slice_sink
        from sliceagent.seed import make_build_slice

        host = _HarborSliceagentHost(sandbox, workdir, timeout=self._timeout)
        llm = OpenAILLM()
        sess = Session(make_memory())
        try:
            llm.set_cache_key(sess.session_id)
        except Exception:  # noqa: BLE001
            pass
        sess.new_topic(instruction)
        record_user(sess.active(), instruction)
        build = make_build_slice(sess, host, _NullRetriever(), make_memory(), instruction, sess.session_id)
        log_f = None
        try:  # reliable host-side path keyed by task (harbor's logs_dir mount wasn't capturing it)
            tdir = os.path.join(os.path.dirname(__file__), "trajectories")
            os.makedirs(tdir, exist_ok=True)
            log_f = open(os.path.join(tdir, f"{self._task}.log"), "w", encoding="utf-8")
        except Exception:  # noqa: BLE001
            log_f = None

        def _log(e):  # diagnosable trajectory: every tool call + observation + assistant reply
            if log_f is None:
                return
            try:
                t = type(e).__name__
                if t in ("ToolStarted", "ToolCall"):
                    log_f.write(f"TOOL {getattr(e, 'name', '?')} {str(getattr(e, 'args', ''))[:300]}\n")
                elif t == "ToolResult":
                    fail = "FAIL " if getattr(e, "failing", False) else ""
                    log_f.write(f"  -> {fail}{(getattr(e, 'output', '') or '')[:400]}\n")
                elif t == "AssistantText":
                    log_f.write(f"SAY {(getattr(e, 'content', '') or '')[:400]}\n")
                elif t in ("TurnEnd", "TurnInterrupted"):
                    log_f.write(f"[{t} {getattr(e, 'stop_reason', getattr(e, 'reason', '?'))}]\n")
                log_f.flush()
            except Exception:  # noqa: BLE001
                pass

        dispatch = make_dispatcher(slice_sink(sess), _log)
        hooks = CompositeHooks(
            CatastrophicSafeguardHook(),
            BudgetHook(4_000_000),
        )
        try:
            result = run_turn(build_slice=build, llm=llm, tools=host, dispatch=dispatch,
                              hooks=hooks, max_steps=self._max_steps)
            return {"usage": result.usage or {}, "steps": getattr(result, "steps", None)}
        except Exception:  # noqa: BLE001 — never crash the trial; a dead turn = a failed task, scored fairly
            return {"usage": {}, "steps": None}


# =================================================================================================
# codex on the ChatGPT subscription, as a harbor agent
# =================================================================================================
_HOST_CODEX_AUTH = Path.home() / ".codex" / "auth.json"


class CodexSubscriptionHarborAgent(BaseAgent):
    SUPPORTS_WINDOWS = False

    @staticmethod
    def name() -> str:
        return "codex-subscription"

    def __init__(self, logs_dir: Path, model_name: str | None = None, task_dir: Path | None = None,
                 trial_paths=None, extra_env: dict | None = None, agent_timeout_sec: float | None = None,
                 **kwargs):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self._logs_dir = Path(logs_dir)
        self._model = (model_name or "gpt-5.5").split("/")[-1]
        self._timeout = int(agent_timeout_sec or 900)

    def version(self) -> str:
        return "1.0.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        if not _HOST_CODEX_AUTH.exists():
            raise FileNotFoundError(f"{_HOST_CODEX_AUTH} not found — `codex login` (subscription) first")
        auth_b64 = base64.b64encode(_HOST_CODEX_AUTH.read_bytes()).decode("ascii")
        script = (
            "set -e\n"
            "apt-get update >/dev/null 2>&1 && apt-get install -y curl >/dev/null 2>&1 || true\n"
            "curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash >/dev/null 2>&1\n"
            'export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"\n'
            "nvm install 22 >/dev/null 2>&1\n"
            "npm install -g @openai/codex@latest >/dev/null 2>&1\n"
            'mkdir -p "$HOME/.codex"\n'
            f'printf %s {shlex.quote(auth_b64)} | base64 -d > "$HOME/.codex/auth.json"\n'
            'chmod 600 "$HOME/.codex/auth.json"\n'
            "echo codex-setup-done\n"
        )
        await environment.exec(f"bash -lc {shlex.quote(script)}", timeout_sec=600)

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        # --json gives a machine-readable event stream we can parse for tokens + step count.
        cmd = (
            'export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh" >/dev/null 2>&1; '
            "codex exec --json --sandbox danger-full-access --skip-git-repo-check "
            f"--model {shlex.quote(self._model)} -- {shlex.quote(instruction)}"
        )
        res = await environment.exec(f"bash -lc {shlex.quote(cmd)}", timeout_sec=self._timeout)
        out = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
        try:
            self._logs_dir.mkdir(parents=True, exist_ok=True)
            (self._logs_dir / "codex-output.txt").write_text(out)
        except Exception:  # noqa: BLE001
            pass
        ti, to, steps = _parse_codex_metrics(out)
        context.n_input_tokens = ti
        context.n_output_tokens = to
        context.metadata = {"steps": steps}
        try:
            import json as _j
            (self._logs_dir / "metrics.json").write_text(_j.dumps(
                {"tokens_in": ti, "tokens_out": to, "steps": steps}))
        except Exception:  # noqa: BLE001
            pass


def _parse_codex_metrics(out: str):
    """Best-effort token + step extraction from `codex exec --json` output (subscription may omit token
    counts; wall time always comes from the trial result regardless). Raw output is saved alongside."""
    import json as _j
    import re

    ti = to = steps = None
    # JSON event stream: count agent turns / command executions; pick the last token_usage seen.
    n_cmds = 0
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = _j.loads(line)
        except Exception:  # noqa: BLE001
            continue
        s = _j.dumps(ev).lower()
        if '"exec' in s or '"command' in s or 'function_call' in s or '"tool' in s:
            n_cmds += 1
        # token usage objects vary by codex version — scan for input/output token ints
        for k, v in _flatten(ev):
            kl = k.lower()
            if isinstance(v, int):
                if "input" in kl and "token" in kl:
                    ti = v
                elif "output" in kl and "token" in kl:
                    to = v
    if n_cmds:
        steps = n_cmds
    # fallback: plain-text "tokens used" / "input tokens: N"
    if ti is None:
        m = re.search(r"input[_\s]*tokens?[\"':=\s]+([\d,]+)", out, re.I)
        ti = int(m.group(1).replace(",", "")) if m else None
    if to is None:
        m = re.search(r"output[_\s]*tokens?[\"':=\s]+([\d,]+)", out, re.I)
        to = int(m.group(1).replace(",", "")) if m else None
    return ti, to, steps


def _flatten(o, prefix=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(o, list):
        for x in o:
            yield from _flatten(x, prefix)
    else:
        yield prefix, o
