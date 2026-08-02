#!/usr/bin/env python3
"""Prove the built core wheel can execute a turn without the product or CLI.

Run this with an isolated interpreter that has only the ``sliceagent-core`` wheel
installed.  CI owns creation of that environment; keeping the behavioral probe here
makes the SDK acceptance contract readable and locally reproducible.
"""
from __future__ import annotations

import importlib.util
from importlib.metadata import requires
import sys
from types import SimpleNamespace


def main() -> int:
    for forbidden in ("sliceagent", "sliceagent_cli"):
        if importlib.util.find_spec(forbidden) is not None:
            raise SystemExit(
                f"standalone-core gate: {forbidden!r} is importable; "
                "the probe environment is not core-only"
            )

    mandatory = [
        requirement
        for requirement in (requires("sliceagent-core") or ())
        if "; extra ==" not in requirement
    ]
    if mandatory:
        raise SystemExit(
            "standalone-core gate: the wheel declares mandatory dependencies: "
            + ", ".join(mandatory)
        )

    import sliceagent_core
    from sliceagent_core import run_turn

    class OneShotLLM:
        context_window = 8_192
        max_tokens = 64

        def complete(self, messages, schemas):
            assert messages == [{"role": "user", "content": "standalone core turn"}]
            assert schemas == []
            return SimpleNamespace(
                content="standalone core completed",
                tool_calls=[],
                finish_reason="stop",
                usage={"prompt_tokens": 4, "completion_tokens": 3},
            )

    class EmptyToolHost:
        def schemas(self):
            return []

    events = []
    result = run_turn(
        build_slice=lambda: [{"role": "user", "content": "standalone core turn"}],
        llm=OneShotLLM(),
        tools=EmptyToolHost(),
        dispatch=events.append,
        max_steps=1,
        # Deliberately no scheduler argument: the ordered scheduler is core turn semantics.
    )
    if result.stop_reason != "end_turn":
        raise SystemExit(
            f"standalone-core gate: expected end_turn, got {result.stop_reason!r}"
        )
    if any(name.startswith(("sliceagent.", "sliceagent_cli")) for name in sys.modules):
        raise SystemExit("standalone-core gate: the turn imported the product or CLI namespace")

    print(
        "standalone-core OK "
        f"(sliceagent-core {sliceagent_core.__version__}, no CLI, stop=end_turn)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
