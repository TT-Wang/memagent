# sliceagent-core

The bounded-slice agent runtime: history-bounded, task-elastic reconstructed context.

This is the reusable engine — no CLI, no coding tools, no terminal UI. It runs one
bounded-slice turn (`SliceAgent.prompt(text) -> TurnResult`) over injected contracts
(model, tools, memory, persistence). Any host (CLI, IDE, web, CI) builds on it.

**The boundary:** `sliceagent_core` never imports `sliceagent_cli`. Enforced in CI
(`scripts/check_import_boundary.py`). That one-way dependency is what makes the runtime
reusable — the moat travels with the core.
