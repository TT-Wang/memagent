# sliceagent-core

The bounded-slice agent runtime: history-bounded, task-elastic reconstructed context.

This is the reusable engine—no CLI, coding tools, or terminal UI. `run_turn(...)` executes
one bounded-slice turn over injected model, tool, retrieval, memory, persistence, safety,
and lifecycle contracts. The ordered scheduler ships with the runtime and is the default;
hosts can still inject another `ToolScheduler`.

Public entry points are `run_turn` and `run_tool_batch`. The host-facing ports are
`LLMClient`, `ToolHost`, `ToolScheduler`, `Retriever`, `Memory`, `Registry`, `Safeguard`,
and `Oracle`; `NullMemory` and `ORDERED_TOOL_SCHEDULER` are included defaults.

A bare install has no required third-party dependencies. The built-in OpenAI-compatible
client uses the optional `openai` extra.

**The boundary:** `sliceagent_core` never imports `sliceagent_cli`. Enforced in CI
(`scripts/check_import_boundary.py`), so the bounded-slice runtime—and its turn
semantics—travels intact with the core.
