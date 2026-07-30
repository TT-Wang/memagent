# Peer-Park Host Design (task #95) — revision 3

Status: **design for review — not implemented.** Revision 3 answers @christina's task #98
review of revision 2 (`2a2026ff`) and adopts the shape she accepted: the park owns
wait/timing and a *reference*, never a payload; the existing durable Raft ingress is the
body authority. Implementation stays paused until this revision is accepted.

C1/C4 are today a well-tested library with **zero production callers**. The conformance
probe is green against the library, not the feature.

## What each revision got wrong (kept deliberately)

- **r1**: enumerated `seal_current` callers in the core repo only, so the bridge — the host
  that actually runs the personas — was left unparked; proposed reaping only at turn
  admission and restart, which cannot fire for a *silent* collaborator; and assumed a resume
  ingress that does not exist (`inject_peer` returns `False` when no turn thread is alive,
  and a parked task is idle, so peer messages to it are dropped).
- **r2**: promised the resumed slice replays the exact `PeerMessage`, which forced a new
  durable body store; filed `message_id` reuse with different content as a benign
  non-resuming outcome when it is **corruption**; and made "durable arrival time governs"
  the expiry rule while the only durable stamp is *local processing* time — a rule that
  would expire the very case it was written to protect.

## D1 — Two production hosts, one pinned pair

| Host | Seal | Change |
|---|---|---|
| CLI | `cli.py::_seal_local_turn` | pass `peer_wait=` from the turn outcome |
| Bridge | `sliceagent_raft/driver.py::HeadlessDriver.finish_turn` (line ~722) | same; `_run_turn` must forward the park, not only `stop_reason`/usage |

`TurnOutcome` gains optional `peer_wait`; every `run_turn`/`TurnOutcome` consumer is
enumerated so `None` provably means "unchanged". **Acceptance is an immutable
`(core_sha, bridge_sha)` pair** — a two-repo feature has no single SHA.

## D2 — Park origin: a typed host operation with an executable control path

A **host-layer** `ask_collaborator` tool (the kernel gains no verb). It must have a real
control carrier, not just a tool result:

- one typed result that the loop recognizes as **turn-ending**, rather than being appended
  as an ordinary tool result while the loop continues;
- **exclusivity**: at most one park per turn; a second call in the same turn is a typed
  error, not a silent overwrite;
- **batch semantics**: co-batching `ask_collaborator` with any **mutating** tool is a typed
  error and the whole batch is rejected atomically, executing nothing. Parking ends the turn,
  so a co-batched mutation has no correct ordering — running it first commits state the model
  may reason about differently after a suspension it did not anticipate, and skipping it
  leaves the model believing it ran. Partial execution across a suspension boundary is state
  nobody can reconstruct later, so the failure is raised loudly at the point of ambiguity.
  **Read-only tools may be co-batched** and execute before the park; they carry no state
  across the boundary;
- **ordering under crash**: the delegation record is prepared before dispatch, so a crash
  between prepare and dispatch is recoverable and cannot orphan either side.

The operation atomically mints **host-derived** `park_id`, correlation, peer identity, and
deadline, issues the `PeerDelegation`, and ends the turn with a parked `TurnOutcome`.

Kernel invariant: `stop_reason == "waiting_peer"` **iff** a typed `PeerWait` is present.

An external orchestrator declaring a park is a **separate API, out of scope** — conflating
it would let the host claim the agent chose to wait when it did not.

## D3 — Body authority: compose existing durable ingress, add no mailbox

- **Body owner: the existing Raft `IngressStore.raw_inbound`.** PersonaHost already persists
  the exact body before scheduling/recovery. No second copy, no new mailbox.
- The park stores a **typed reference only**: the immutable ingress message id bound to
  `park_id`/generation, peer, correlation, artifact digest, and arrival.
- **Binding goes through the host-authenticated typed ingress lane** — never thread affinity
  and never a history search. A reply's meaning depends on addressing, not location; an
  agent-authored reply in an existing thread is legitimately ignorable as an echo.
- On resume the host **rehydrates a typed peer envelope exactly once** and records a typed
  delivery receipt. The ordinary idle path is insufficient: it renders messages as
  `_message_text`, dropping `message_id`/`correlation_id`/`wake` — precisely the fields the
  causal binding needs.
- Evidence stores stay body-free; this changes nothing about them.

### Message dispositions

| Case | Resumes? | Agent-visible? |
|---|---|---|
| matching reply, at or before deadline | yes, exactly once | yes, exactly once |
| `wake="none"` (e.g. freeze/revocation) | never | **yes** — and the deploy boundary enforces it host-side |
| wrong peer / wrong correlation / stale / post-expiry / cancelled | never | yes, once |
| exact duplicate (same id, same immutable content) | no | **no second delivery** — idempotent |
| same `message_id`, different content | no | **corruption — fails loudly** |

"Non-resuming" never means discarded: only the *resume transition* is withheld.

## D4 — Clock: one domain end to end

`raw_inbound.received_at` is **local processing time** and the parser does not retain the
server timestamp, so it cannot decide expiry: after a restart a timely reply is stamped late.

The contract is **two-part**, because "was the reply timely?" and "what wakes a silent
wait?" are different problems and only the first is a comparison.

**(a) Eligibility and ordering — immutable server event timestamps.**
- Preserve the trusted **server/event timestamp** through the parser into the durable ingress
  row, at full precision.
- **Do not translate host wall-clock park time into server time.** Anchor the deadline to an
  authoritative server-stamped event — the outbound delegation's own Raft timestamp — and
  persist `deadline_at = delegation_server_time + duration`. Translation would reintroduce
  the local clock as a correctness input through the back door.
- The reply's immutable server timestamp then compares **directly** to `deadline_at`.
  **Inclusive boundary preserved**: only `arrival > deadline_at` expires.
- **Until the delegation is server-confirmed, the bounded park is not fully armed** — there is
  no authoritative anchor yet, and this state must be explicit rather than assumed.

**(b) Liveness wake — server-domain now, idempotently fenced.**
- A comparison rule cannot wake anything. The out-of-band scheduler needs an authoritative
  notion of **server-now**: a server-side deadline event/reminder, or a trusted server-time
  query, is the correctness source.
- **Local monotonic timers are wake accelerators only, never authoritative.** On recovery,
  compare trusted server-now against the stored server-domain `deadline_at` and CAS the same
  transition. Without this, a local clock rollback still delays expiry even though reply
  classification is sound.

Local processing time is operational telemetry and never a decision input. Measuring against
host-local park start is rejected: it charges the collaborator for *our* restart.
`deadline_s=None` is intentionally unbounded — no timer, never reaped by time.

**Platform dependency, stated explicitly:** this removes local wall time from correctness
entirely, *provided* Raft guarantees immutable comparable event timestamps plus a
server-now/deadline firing mechanism. If it cannot, the explicit rollback fallback stays and
the weaker guarantee must be documented rather than silently assumed.

## D5 — Coordinator, saga, and fencing

Ownership, since these stores cannot share a transaction:

- **transport/ingress** owns durable arrival and replay identity;
- a host **`PeerWaitCoordinator`** owns park/resume/expire/cancel transitions;
- the **request-root checkpoint is authoritative** for the wait and its timing;
- scheduler and mailbox indexes are **derived, replayable projections** — never second
  sources of truth.

Minimum saga (each step idempotent, keyed by `park_id`/generation):

`delegation prepared (durable, pre-dispatch)` → `dispatched` → `server-confirmed → deadline armed` → *[wait]* → `raw ingress persisted` → `host match decision bound to park_id/generation + peer/correlation/artifact_digest/arrival` → `graph CAS resume-or-expire` → `durable schedule/delivery outbox entry` → `deterministic turn admission` → `typed semantic-input receipt`

The outbound prefix and the post-CAS tail are load-bearing, not bookkeeping. **prepared →
server-confirmed → armed** makes "not yet armed" an *observable* state: a crash between
prepare and confirm must recover to unarmed, never to a silently-bounded park whose deadline
is anchored to nothing. The **post-CAS outbox** closes the window where a crash between the
CAS and the scheduling would lose the wake entirely, or double it on retry.

- **Resume and expire are mutually exclusive**, decided by CAS against graph revision.
- **Park identity** is a host-minted `park_id` scoped to checkpoint/binding, with generation
  tombstones so an id can never be reused or resurrected.
- **Timers and scheduled turns are leased and fenced**, so a duplicate or restarted owner
  cannot double-schedule.
- **Supersede emits an explicit, idempotent cancellation** of the outstanding delegation and
  reconciles it — not merely making a later reply stale.
- The out-of-band wake fires **without inbound traffic**, is restored on restart with
  already-overdue parks reaped immediately, and **schedules** the newly live request;
  flipping graph status alone does not run work.

## Acceptance

1. **Mechanical caller enumeration across both repos and both hosts**, with the exact
   `(core_sha, bridge_sha)` pair.
2. Real bridge path: typed delegation → parked `TurnOutcome` → both seals persist graph and
   timing.
3. Reply at or before deadline → resume once → **typed** envelope appears exactly once with
   `message_id`/`correlation_id`/`wake` intact → task completes.
4. Silence, no other traffic → out-of-band wake → expiry → work **scheduled** live again.
5. Restart before deadline; restart after deadline; **reply that arrived before the deadline
   but is processed after recovery still resumes** (the clock-domain case). Plus: a local
   clock rollback must not delay expiry, and a park whose delegation is not yet
   server-confirmed must report as not-yet-armed rather than silently bounded.
6. Exact-boundary, post-expiry, wrong-peer, wrong-correlation, `wake="none"` visibility,
   duplicate idempotence, **`message_id` reuse failing loudly**, out-of-order, cancel,
   supersede-cancel reconciliation, concurrent parks.
7. **Crash injected after every saga phase** — delegation prepared, dispatched, server-confirmed,
   deadline armed, graph parked, ingress persisted, match decided, graph resumed/expired,
   schedule outbox committed, turn scheduled, receipt committed —
   converging with no lost delegation, no park without delegation, no double payload, no
   double turn.
8. **Revert-red per wiring**: bridge seal, durable timer, idle ingress, scheduler wake,
   cleanup — each removal turns a *named* test red.
9. Evidence must traverse the **production transport and host**; a unit/harness shortcut does
   not satisfy the end-to-end gate.
10. **Eval 1 traverses the durable Raft/PersonaHost ingress**, not the transient-body
    `SwarmPeerRouter` path — otherwise it tests a route production does not use.

## Non-goals

- No new **kernel** verb; no clock inside the kernel.
- No change to the frozen C1/C2/C4 conformance contract — this adds callers, not semantics.
- No new body store.
- No claim of human-likeness from a synthetic collaborator: task #97 proves host coordination
  competence; human-likeness needs real-human traces.
