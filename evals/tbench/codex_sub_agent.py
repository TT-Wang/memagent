"""Codex on the ChatGPT SUBSCRIPTION (not a pay-per-token API key).

The stock terminal-bench `codex` agent writes {"OPENAI_API_KEY": ...} into the container — billed per
token. This subclass instead ships the HOST's ~/.codex/auth.json (the ChatGPT-subscription OAuth tokens)
into the task container, base64'd through the agent env, so the in-container `codex exec` authenticates
via the subscription. Everything else (install, `codex exec --model …`) is the stock agent.

Run (from repo root):
  PYTHONPATH=src:. .venv/bin/tb run --dataset-path <tb2-tasks> -t <id> \
    --agent-import-path evals.tbench.codex_sub_agent:CodexSubscriptionAgent --model openai/gpt-5.5
"""
from __future__ import annotations

import base64
from pathlib import Path

from terminal_bench.agents.installed_agents.codex.codex_agent import CodexAgent

_HOST_AUTH = Path.home() / ".codex" / "auth.json"


class CodexSubscriptionAgent(CodexAgent):
    @staticmethod
    def name() -> str:
        return "codex-subscription"

    @property
    def _env(self) -> dict[str, str]:
        # NO api key — carry the host's subscription auth.json (OAuth tokens) into the container instead.
        # base64 so the multi-line JSON survives `export KEY='...'` cleanly.
        if not _HOST_AUTH.exists():
            raise FileNotFoundError(f"{_HOST_AUTH} not found — run `codex login` (subscription) first")
        return {"CODEX_AUTH_B64": base64.b64encode(_HOST_AUTH.read_bytes()).decode("ascii")}

    @property
    def _install_agent_script_path(self) -> Path:
        return self._get_templated_script_path("codex-sub-setup.sh.j2")
