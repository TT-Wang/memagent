# Peer-Park Host Design (task #95) — revision 2

Status: **design for review — not implemented.** Revision 2 answers the five blocking
findings in @christina's task #98 review of revision 1 (`d436cbb`). Implementation stays
paused until the choices below are accepted.

C1/C4 are today a well-tested library with **zero production callers**: nothing parks,
nothing resumes, nothing expires. The conformance probe is green against the library, not
the feature.

## What revision 1 got wrong

Recording this, because two of the misses are the same failure class we keep hitting:

1. I enumerated `seal_current` callers **in the core repo only** and declared one production
   seal. The bridge — the host that actually runs the personas — has a second
   (`HeadlessDriver.finish_turn`). My own acceptance item was "mechanical caller
   enumeration" and I scoped it to one repo. Cross-repo composition, again.
2. I proposed reaping at turn admission and restart. A **silent** collaborator produces
   neither event, so the one case the reaper exists for was the one it could not catch.
3. "Host-declared parking" named no typed path from a delegation to a parked outcome, so
   the host would have been inferring the agent's intent.
4. **`HeadlessDriver.inject_peer` returns `False` when no turn thread is alive, and a parked
   task is idle.** The C2 delivery lane and the park lane do not connect in production at
   all. Revision 1 assumed a resume ingress that does not exist.

## Design

### D1 — Two production hosts, one pinned pair

Both seals propagate the typed park:

| Host | Seal | Change |
|---|---|---|
| CLI (local) | `cli.py::_seal_local_turn` | pass `peer_wait=` from the turn outcome |
| Bridge (durable personas) | `sliceagent_raft/driver.py::HeadlessDriver.finish_turn` | same, and `_run_turn` must forward the park, not only `stop_reason`/usage |

`TurnOutcome` gains an optional `peer_wait`. Every `run_turn`/`TurnOutcome` consumer
(including scoped-child and alternate hosts) is enumerated so the added field cannot
silently change their behaviour — default `None` must mean "unchanged".

**Acceptance is an immutable `(core_sha, bridge_sha)` pair.** A two-repo feature has no
single SHA; a lone core pin cannot prove the composition.

### D2 — Park origin: one typed host operation, no inference

A **host-layer** tool (`ask_collaborator`) is model-visible; the kernel gains no new verb.
One atomic operation:

1. mints correlation, peer identity, and deadline **host-side** (never model-authored — see
   the durable-evidence rule the bridge now enforces),
2. issues the `PeerDelegation`,
3. ends the turn with a parked `TurnOutcome` carrying the typed `PeerWait`,
4. binds the delegation to the current **logical work identity**, not just a correlation.

Result-boundary invariant, enforced in the kernel:
`stop_reason == "waiting_peer"` **iff** a typed `PeerWait` is present.

An external orchestrator declaring a park (rather than the agent choosing to wait) is a
**separate API, explicitly out of scope here** — conflating them would let the host claim
the agent chose to wait when it did not.

### D3 — Durable timing in the same fact as the park

One record, committed and recovered with the graph in a single transaction:

- **Key**: `(logical_work_id, correlation_id)` — a bare correlation is not a stable identity.
- **Fields**: durable start time, deadline duration, and the scheduled wake.
- **Paired recovery invariants**: a bounded `waiting_peer` record may not lack timing, and
  timing may not survive resume, expiry, cancel, supersede, or retirement.
- **Clock**: a durable monotonic-with-wall-fallback representation, with defined behaviour
  under restart and clock rollback. The kernel still receives only explicit elapsed values
  and reads no clock.
- `deadline_s=None` is **intentionally unbounded** — no timer is scheduled and the reaper
  never expires it.

### D4 — Out-of-band deadline wake

A durable timer/sweep that fires **without inbound traffic**:

- scheduled at park commit; **restored on restart**, with already-overdue parks reaped
  immediately;
- on expiry: transition to `in_progress` with `peer_wait_expired` **and schedule the request
  for another turn** — changing graph status alone does not run work, which was the gap in
  revision 1;
- acceptance runs on a **virtual clock / deterministic event queue**; wall-clock and API
  latency are a separate operational measurement, never the thing tests depend on.

### D5 — Idle ingress and an atomic resume-XOR-expire state machine

The blocking gap: **a parked binding is idle, and `inject_peer` refuses when idle.**

- **Persist first, resume second.** C2 input is durably recorded on arrival *even when the
  binding is idle or parked* — persistence must not depend on a live turn thread. Resume is
  then attempted from durable state, not from an in-flight injection.
- **Exactly one transition wins.** Resume and expire are decided atomically against graph
  revision and durable event time; the wait and its timer are consumed exactly once, and
  exactly one resumed turn is scheduled with the peer payload delivered exactly once.
- **Durable arrival time governs**, not processing time — so a reply that arrived before the
  deadline but is processed after a restart still resumes.
- **Boundary rule preserved**: a matching reply at exactly the deadline is accepted; only
  `elapsed > deadline` expires (consistent with `correlate_peer_result`).
- **Resume by exact logical-work identity**, requiring active-correlation uniqueness.
- Non-resuming typed outcomes, never errors and never resurrection: `wake="none"`, wrong
  peer, wrong correlation, duplicate/conflicting `message_id`, stale, cancelled/superseded,
  and post-expiry replies.

## Acceptance

1. **Caller enumeration across both repos and both hosts**, with the exact `(core_sha,
   bridge_sha)` pair. Mechanical, not by inspection.
2. Real bridge path: typed delegation → parked `TurnOutcome` → **both** seals persist graph
   *and* timing atomically.
3. Reply after park, at or before deadline → resume once → payload appears exactly once in
   the resumed slice → task completes.
4. No reply, no other traffic → out-of-band wake → expiry → work is **scheduled live again**.
5. Restart before deadline; restart after deadline; reply persisted before a crash and
   processed after recovery.
6. Exact-boundary, post-expiry, wrong-peer, wrong-correlation, `wake="none"`, duplicate,
   conflicting-ID, out-of-order, cancel, supersede, concurrent-park.
7. **Revert-red per wiring**: removing the bridge seal, the durable timer, the idle ingress,
   the scheduler wake, or the cleanup each turns a *named* test red.
8. A unit/harness shortcut **does not** satisfy the end-to-end gate — evidence must traverse
   the production transport and host.

## Non-goals

- No new **kernel** verb (the host-layer tool is not a kernel verb).
- No clock inside the kernel — elapsed readings stay host-supplied and explicit.
- No change to the frozen C1/C2/C4 conformance contract; this adds callers, not semantics.
- No claim of human-like collaboration from a synthetic collaborator: task #97 can prove
  host coordination competence; human-likeness needs real-human traces (per #98).
