# sliceagent-cli

The SliceAgent coding agent — a thin CLI/TUI host built on `sliceagent-core`.

It injects the coding domain into the core: coding tools (`ToolHost`), the Memem memory
stack (`Memory`), workspace retrieval (`Retriever`), and renders the core's events to a
terminal. It depends on `sliceagent-core`; the core never depends on it.

Entry point: `sliceagent = sliceagent_cli.cli:main`.
