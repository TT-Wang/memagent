# Subagent System — Direct Outcome Architecture

> Status: runtime contract for 0.3.x · 2026-07-18

## Thesis

A child is an isolated sliceagent computation, not a workflow node and not a second memory system.

> The child receives a brief, runs privately, and returns one complete normalized report through the
> ordinary tool-result channel. UI and persistence observe the return; neither delivers it.

“Bounded” means the child trajectory never accumulates in the parent — not that every report has a tiny
fixed size. The report may expand to the task's legitimate complexity; the private transcript never crosses
the boundary.

## One tool

```text
spawn_agent(agent="explorer", task="…", scope=[…], exclusions=[…])
```

- `agent` — the kind to run; an enum built live from the agent registry (required).
- `task` — the self-contained sub-task (required). The brief carries the task, still-binding standing user
  constraints, scope, and exclusions — never the parent transcript or fan-out mechanics.
- `scope` / `exclusions` — optional exact path/question sets and explicit exclusions.

Nothing else: no names, no grants, no report shapes, no drift policies, no work-item bookkeeping. The
former `spawn_explore` / `spawn_subagent` aliases are gone; `spawn_agent` subsumes both.

## Kinds

Builtins: `explorer` (read-only investigation), `general` (writable worker), `reviewer`, and
`verification`. User-defined `agents/*.md` files (markdown + frontmatter) are always loaded from skill
roots, the repo root, and `.sliceagent/` — there is no env gate. The live schema's `agent` enum is
authoritative. Read-only kinds may overlap in the scheduler; writable kinds serialize behind a mutation
barrier.

## Child lifecycle

Every spawn is a one-shot temp with a fresh context slice. The child runs ONE ordinary tool-enabled
model/tool loop — the same turn machinery as the parent, with no staged navigation/synthesis phases and no
reserved final pass. It is bounded by:

- the per-turn step ceiling (`AGENT_MAX_STEPS`);
- the delegation wall-clock deadline `AGENT_DELEGATION_TIMEOUT` (default 900s);
- a concurrency cap: the scheduler admits children in waves under lifecycle slots and the process-wide
  provider inflight lease.

Depth is `subagent_depth` (config default 1): children cannot spawn children unless it is raised.

## Acceptance: trust the report

The parent TRUSTS the child's final report — the same acceptance policy as every peer agent (Claude Code,
opencode, Amp, Goose):

- clean `end_turn` + non-empty report → `succeeded`;
- `max_steps` ceiling + non-empty accepted report → `partial` (the spawn operation returns ok with the
  deliverable; the ceiling stop is recorded in the outcome labels and the artifact gaps, never as an error);
- empty report → `failed`;
- interrupt/cancellation → `cancelled` (or `indeterminate` when physical closure is unconfirmed); any
  preserved partial material is explicitly labeled partial, never re-rolled into a plausible report.

Evidence (navigation vs content observations) is recorded as an informational label on the seal and in the
report envelope. It is NEVER an acceptance gate: a navigation-only explorer that answers a mapping/listing
question from `list_files`/`glob` has done its job.

## The whole path

```text
parent model calls spawn_agent → scheduler admits under child/provider limits → child runs in a fresh
slice → ChildOutcome returns as the spawn_agent tool result → parent sees the full report and synthesizes;
TUI and artifact/memory stores observe and persist opportunistically.
```

There is no second fan-in protocol. The scheduler already preserves provider-call order even when children
finish out of order; that ordered list of tool results IS the fan-in. The report body appears exactly once
in model context.

## Seal and recovery surface

A settled child is sealed as a plain canonical record — report, bounded observations, gaps, uncertainty,
usage — in the artifact store with a memory mirror. It is re-readable via `subagents/sub-N.md` (SubagentFS)
as a recovery/refinement surface, not the delivery channel: in normal execution the full report reaches the
parent without an artifact read. No claims, no sha256 observation checksums, no grants, no standing
specialists, no fan-in bundles.

Persistence is an observer: artifact-store, index, or memory-mirror failure after a safe report exists
preserves the computed status and adds a persistence warning; it cannot relabel completed computation.

## Active Work separation

Active Work tracks user commitments needing cross-turn continuity, not child processes: `spawn_agent`
requires no Active Work item, child settlement never mutates the graph, and open items never block an answer.

## Removed machinery

Cut in the simplification — over-engineered relative to every peer, and in one case actively harmful:

- **Evidence gate** — evidence-free explorer reports were rejected even when they were correct; every peer
  trusts the child's final message. Evidence is now informational only.
- **Staged explorer** — the two-phase navigation-then-synthesis profile (and its `AGENT_EXPLORER_REASONING`
  / `AGENT_EXPLORER_NAV_STEPS` knobs) is one ordinary loop now.
- **Grants and roster** — exact artifact grants, hire/wake standing specialists, and roster careers added a
  second artifact-routing protocol with no remaining consumer.
- **Advanced-agents gate** — `AGENT_ADVANCED_AGENTS` made writable/named kinds an env-gated mode; the kinds
  are just registry entries now.
- **Fan-in bundles** — the parent synthesizes from ordered tool results; no synthetic packet.
- **Synthesiser kind** — its only input channel was grants: children cannot read the parent's `subagents/`
  namespace, so after grants were cut it could never read the reports it existed to merge. The parent
  synthesizes (bounded reads page out behind handles); restore a narrow same-session seal-read channel only
  if wide fan-outs ever return.
- **Token-budget splitting** — children are bounded by steps and the wall-clock deadline only.
- **Legacy aliases** — `spawn_explore` / `spawn_subagent`.

## Non-negotiable invariants

- One child call produces one ordered tool result.
- The full accepted report reaches the parent without an artifact read; the report body appears exactly once
  in model context.
- UI and persistence never decide whether computation is delivered.
- Child lifecycle never mutates user-commitment state.
- Storage failure cannot relabel completed computation.
- Actual unresolved physical effects remain indeterminate.
- Parent synthesis preserves failed/partial coverage and verifies material claims proportionally.
