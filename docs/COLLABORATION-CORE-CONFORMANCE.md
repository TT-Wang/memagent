# Collaboration core conformance seams

This is the core-only seam map and RED acceptance contract for the first peer
collaboration primitives.  Run:

```bash
PYTHONPATH=src python evals/collaboration_core_conformance.py
```

The command exits non-zero until all three contracts exist.  It emits one JSON
record per contract plus a summary, so an eval runner can localize a failure
without interpreting prose.

## C1 — durable `waiting_peer`

Current seams:

- `active_work.WorkStatus`, `WORK_STATUSES`, `UNRESOLVED_STATUSES`, and
  `_ALLOWED_TRANSITIONS` own the durable lifecycle vocabulary.
- `WorkGraph.seal_current` special-cases only `waiting_user`; a
  `waiting_peer` stop with a response is currently misclassified as delivered.
- `regions.render_convergence` applies read/edit convergence pressure without a
  peer-wait exemption.
- `tools.update_work`, the Active Work context renderer, progress projection,
  and TUI status rendering each enumerate the same lifecycle separately.

Acceptance contract:

1. `waiting_peer` is a durable unresolved Active Work status.
2. `seal_current("waiting_peer", response_ref)` parks rather than delivers.
3. the park carries a non-empty correlation ID and optional peer/deadline
   metadata; peer correlation is state, not prose in `stop_reason`.
4. convergence renders no finish/keep-working nudge while the current request
   is parked on a peer.
5. only a matching correlated peer result resumes the request.

## C2 — typed peer input on the steer/admission lane

Current seams:

- `loop.run_turn(..., steer_queue=...)` accepts arbitrary values, coerces every
  value to text, and appends every admitted item as a plain user-role message.
- `_drain_steers` emits only `events.SteerDelivered`; consumers cannot
  distinguish a human steer from a peer message.
- the two drain boundaries (step boundary and pre-finalization) are the correct
  sequence-safe admission points and must remain the only drain sites.

Acceptance contract:

1. the public core interface has a typed `PeerMessage` carrying message ID,
   sender, content, correlation ID, and a wake contract.
2. `run_turn` admits it through the existing steer queue at the existing safe
   boundaries.
3. admission emits a typed peer-delivery event distinct from
   `SteerDelivered`.
4. the next provider call receives a bounded, attributable peer envelope; it is
   not indistinguishable from human input.
5. a peer message whose wake correlation does not match the parked request
   cannot silently resume that request.
6. delivery and resumption remain separate: `wake="none"` allows either an
   empty correlation ID or a correlated informational message, while
   `wake="resume_wait"` requires a non-empty correlation ID. Core C2 admits the
   typed input but does not itself resume durable parked work.

## C4 — correlated delegation return

Current seams:

- `scoped_agent.ScopedResult` and
  `scoped_spawn.ScopedSpawnHost._effects` provide a strong vertical child
  outcome, keyed by invocation identity.
- there is no horizontal `PeerDelegation` request/result type, peer target,
  reply correlation, deadline, or cancellation lifecycle in core.
- bridge-specific handoff/return records therefore cannot be expressed as a
  kernel effect without collapsing their identity into prose.

Acceptance contract:

1. core exposes typed `PeerDelegation` and `PeerResult` records.
2. both carry the same non-empty correlation ID; delegation also carries target
   peer and deadline, and result carries source peer plus typed outcome status.
3. a result with the wrong correlation ID does not satisfy or resume the wait.
4. a matching result produces one terminal correlation outcome and makes the
   report addressable without parsing prose.
5. expired/cancelled delegations cannot be resurrected by a late result.

## Localization rule

These probes import only `sliceagent` core and use an in-memory queue and
scripted model.  No Raft bridge, transport, model judgment, filesystem, or
network is involved.  Therefore each initial failure mechanically localizes to
the core/interface layer.  Once implementation turns the probes green, reverting
the corresponding production primitive must make its probe red again.
