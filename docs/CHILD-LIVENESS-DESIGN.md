# Child liveness: inactivity-based delegation deadline (design)

Status: proposed (#33 review P0 #4) · 2026-07-27 · implementation owner: TBD (scheduler seam — coordinate
with the steer/lease work)

## Problem

Every delegation wave has a fixed wall-clock ceiling (`AGENT_DELEGATION_TIMEOUT`, default 900 s,
non-disableable; `scheduler.py` sets `deadline = monotonic() + timeout` at wave start). A child that is
*actively streaming, running tools, or sealing its report* is still cancelled at 15 minutes — the metric
confuses "working for a long time" with "stuck". Peers have moved to inactivity semantics (Hermes: 900 s
inactivity reset by streamed tokens/tool events; Kimi: 30-minute subagent ceiling).

## Design

**Metric change:** the 900 s default becomes an **inactivity window** reset by child activity, plus an
absolute leak guard.

1. **Activity source.** `ScopedSpawnHost._ProgressEmitter` observes every child lifecycle event
   (StepBegin / ModelCallPrepared / AssistantText / ToolStarted / StepEnd). `touch()` the activity
   cell at the TOP of `__call__`, **before** the identical-(phase, detail) dedup in `_publish` — a
   child steadily working inside one phase still proves liveness even when no new UI row is emitted.
   Transport-level liveness is **mandatory, not optional**: the child's LLM view currently clears
   `transport_activity`, so a child inside one long model call would look dead — the scoped child's
   transport must wire its `LLM_STREAM_HEARTBEAT_SEC` events (or byte-level read activity) into the
   same cell before this design ships.
2. **Carrier.** Reuse the private-arg protocol (`execution.py` `CHILD_*_ARG`): the loop injects a
   `__sliceagent_activity__` cell (a small object with `touch()`/`last`) into `spawn_agent` call args,
   exactly as `_ChildCancellationLease` is injected today. `ScheduledTool` gains an optional
   `activity` reference to the same cell. The cell is **initialized to the admission time** at
   injection, so a child hung before its first event still times out from admission, not from epoch.
3. **Scheduler check — per child, never pooled.** Each lifecycle job's cutoff is computed from ITS OWN
   cell: `job_deadline = min(wave_start + AGENT_DELEGATION_ABSOLUTE, job.activity.last +
   inactivity_window)`. A `max(...)` across jobs would let one healthy child keep a hung sibling alive
   until the absolute guard — the exact confusion this design removes. A job past its own deadline is
   cancelled individually (same lease/grace path as today); the wave continues for live siblings.
   `AGENT_DELEGATION_ABSOLUTE` is a new env (default 3600 s, never disableable — `0`/invalid → default,
   the same fail-closed posture as today). Ordinary read deadlines are untouched.
4. **Reporting.** The timeout classification text distinguishes "no activity for {window}s"
   (inactivity) from "absolute {guard}s ceiling" (leak guard), keeping INDETERMINATE semantics for
   writable children exactly as today.
5. **Config.** `AGENT_DELEGATION_TIMEOUT` keeps its name and 900 s default but is documented as the
   inactivity window; `AGENT_DELEGATION_ABSOLUTE` (3600 s) is the new leak guard. Invalid values revert
   to defaults; neither can be disabled.

## Invariants to preserve (why this is a careful change)

- The lifecycle lane's lease/cancel/grace machinery is history-hardened (the "agents starting forever"
  freeze). The activity cell must be advisory input to the deadline computation ONLY — no new blocking,
  no lease lifetime changes, no change to `request_cancel` composition or grace classification.
- A child that emits NO events (hung before its first step) still dies at the inactivity window —
  identical to today's behavior at 900 s.
- `0`/invalid never means unbounded (durable cancellation/recovery is not yet proven — report §7).

## Tests

1. Synthetic wave where a child touches the cell every ~1 s for > window duration → survives past the
   old wall-clock ceiling and completes.
2. A silent child (no touches) → cancelled at the inactivity window, same classification as today.
3. An active child at the absolute guard → cancelled with the leak-guard classification.
4. Invalid env values (`0`, negative, junk) revert to 900/3600.
