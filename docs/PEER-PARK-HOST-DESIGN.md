# Peer-Park Host Design (task #95)

Status: **design for review — not implemented.** C1/C4 are currently a well-tested
library with **zero production callers**: nothing parks, nothing resumes, nothing expires.
The conformance probe is green against the library, not the feature. This document is the
plan to close that, circulated before code so the design can be attacked cheaply.

## What exists today

| Piece | State |
|---|---|
| `PeerWait` / `PeerResult` / `PeerDelegation` | typed, hardened, fully validated |
| `WorkGraph.seal_current(..., peer_wait=, resolve_peer_wait=)` | park is durable, preserved across every seal branch, explicit resolution |
| `active_work.resume_waiting_peer(graph, PeerResult)` | correlation + expected-peer gated |
| `active_work.expire_peer_waits(graph, elapsed_by_correlation)` | deterministic reaper, host supplies elapsed |
| `interfaces.correlate_peer_result(delegation, result, elapsed_s=)` | correlation + peer + deadline gated |
| C2 `PeerMessage` delivery into a running turn | working, with `wake="none"|"resume_wait"` |
| **A production caller for any of the above** | **missing — this document** |

## The five missing pieces

### 1. A turn can end parked
`run_turn` has no path that produces `stop_reason="waiting_peer"`. Proposal: the kernel
surfaces an optional `peer_wait` on its turn result, set when the turn ends because it is
waiting on a peer. Two candidate triggers, in preference order:

- **(a) Host-declared.** The host, holding the delegation it just issued, tells the loop the
  turn is peer-blocked. No new model-visible surface; the kernel stays mechanism-only.
- **(b) Tool-declared.** A registered `await_peer` tool records a `PeerWait`; the loop ends
  the turn with `stop_reason="waiting_peer"`.

**(a) is preferred**: parking is a lifecycle fact the host owns, and it keeps the kernel free
of another model-facing verb. (b) can be layered later without changing the graph contract.

### 2. The seal site passes it through
`cli.py` `_seal_local_turn` is the single `seal_current` caller and never passes `peer_wait=`.
It must forward the turn result's park. The lifecycle work is already done: a park now
survives re-seal, response delivery, workspace transition, and retirement.

### 3. Durable park-start timing
`PeerWait.deadline_s` is a **duration**, and the kernel deliberately reads no clock, so the
host must record when each park began and supply `elapsed_by_correlation` to the reaper.
This must be **durable** — a park that survives a restart but loses its start time can never
expire. Proposal: persist `{correlation_id: started_at}` alongside the checkpoint that
already carries the work graph, so park and timing are restored atomically.

### 4. Resume trigger from a C2 peer message
C2 already delivers `PeerMessage(correlation_id, peer_id, wake="resume_wait")`. The host
correlates an arriving resume-eligible message into a `PeerResult` and calls
`resume_waiting_peer`. Authority rules that must hold at this seam:
- `wake="none"` is ordinary delivery and must **never** resume;
- correlation **and** `peer_id` must both match (already enforced downstream — the host must
  not pre-empt or weaken it);
- a non-matching message is ordinary peer input, not an error.

### 5. Reaper cadence
`expire_peer_waits` must be invoked somewhere real. Proposal: at turn admission, before the
slice is built — a park is only interesting when we are about to do work — plus on restart
recovery. An expired park returns to `in_progress` with `peer_wait_expired`, so the request
is live again and the convergence exemption stops suppressing nudges.

## Acceptance (what would make this a feature, not a library)

1. **Caller enumeration**: `resume_waiting_peer`, `expire_peer_waits`, and
   `seal_current(peer_wait=)` each have at least one production caller. This is the check that
   caught the current state; it must pass mechanically.
2. **End-to-end**: park → peer replies late → resume → turn completes.
3. **End-to-end**: park → peer never replies → reaper expires it → work becomes live again,
   convergence resumes.
4. **Restart**: park survives a restart *with its timing* and can still expire afterwards.
5. **Authority**: a `wake="none"` message does not resume; a wrong-peer reply does not resume.
6. **Revert-red**: removing any one of the five wirings turns a named test red.

A pseudo-human collaborator eval (task #97) is the natural end-to-end case for 2–4: an
LLM-played human is slow, late, and sometimes silent, which is precisely the behaviour these
primitives exist to survive. Preference is to build the host **to** that eval rather than
write the eval afterwards to fit the host.

## Non-goals

- No new model-facing verb in this pass (option 1b stays deferred).
- No clock inside the kernel — elapsed readings stay host-supplied and explicit.
- No change to the frozen C1/C2/C4 conformance contract; this adds callers, not semantics.
