# Session Spine — Roadmap Spec

Status: ACTIVE. Owner: TT-Wang. Started 2026-08-04. Companion docs:
`SESSION-SPINE-DESIGN.md` (architecture) · `SESSION-SPINE-MAP.md` (recon) ·
`SESSION-SPINE-REVIEW.md` (adversarial verdict, R1–R10).

## North star

One lane, one schema. The prompt is built by ONE assembler from blocks of exactly two kinds:

- **frozen** — sealed bytes, rendered once at seal, ordered by seal order, never re-rendered
- **live** — rebuilt this turn, small, ordered by a fixed tail layout

`prompt = [system, epoch-pinned] [HEAD: durable knowledge] [SPINE: frozen turn digests]
[TAIL: live slice + current ask last] [trajectory]`

Non-negotiable invariants, in priority order:
1. User authority: verbatim (post-redaction) user wording is never paraphrased; the paired
   verbatim reserve survives (R8) and the spine subsumes only turns older than it.
2. Bounded peak: spine + compaction keeps per-call peak history-bounded (the moat).
3. Byte honesty: frozen means frozen — a renderer upgrade must not rewrite history (R1);
   every gate is pre-registered; the byte gate precedes the quality gate.
4. One path: both former lanes consume identical frozen entries through one assembler;
   a lane-parity test guards the seam until the legacy lane is deleted.

## Phases

### P0 — Debt deletion ✅ DONE (`904914f`)
Deleted the four dead layout branches (cache/v2/v3/m1). `AGENT_FREEZE_OPEN_FILES` semantics
graduate into the TAIL definition (turn-start snapshot).

### P1 — Design v2 (fold the verdict)
Rewrite DESIGN.md around the one-lane target with every review requirement resolved:
- R1: digest = field of `safe_record`, rendered inside `_seal_locked` after the indeterminate
  upgrade; resume = scan `kind=='turn'` artifacts (subagent-* filtered), order by
  `artifact_order_key`, concatenate stored strings verbatim.
- R2: digest inputs are post-redaction, one dialect (`preserve_length=True`).
- R3: ONE renderer, journal-derivable (works for crash recovery), byte-parity test specced.
  Degenerate cases specced: abort-during-prep, `waiting_peer` marked open.
- R4: machine locators cite `artifact_id`; any turn number is display text.
- R5 DECISION REQUIRED: segment model — per-segment entries (dedup repeated ask) vs
  per-logical-turn at terminal segment; cross-workspace carrier via `workspace_continue`.
- R7: degradation = whole-spine → single index locator, per-turn latch, spine priority above
  the volatile tail; oscillation probe specced.
- R8: paired reserve retained; spine boundary = reserve floor.
Exit gate: design v2 committed; every R-item either resolved in text or explicitly deferred
with rationale.

### P2 — Write side (digest at seal)
Implement the single renderer + `safe_record` field + resume scan.
Exit gates (all mechanical, no LLM):
- unit: digest determinism (same journal → same bytes), idempotency per artifact_id
- byte-parity: seal-path render == journal-only recovery render, per turn, on a scripted session
- redaction: seeded secret in the ask → absent from spine bytes and artifact alike
- resume: restart mid-session → byte-identical spine (single-workspace scope per R5 decision)

### P3 — Head stability prerequisites (R6; spine is worthless without this)
- system prefix: epoch-pinned (compute once per session/epoch); live git line moves to TAIL
  (`worktree` region already carries live git)
- repo map: pinned per epoch
- memory region: snapshot per topic/session (not re-queried per request), or relocate behind
  the spine into TAIL
Exit gate: byte-diff assertion in the instrumented probe — system message and HEAD bytes
identical across consecutive turns of a scripted session (edits allowed, no topic switch).

### P4 — Read side (one assembler)
- spine block emission: one `alternative_group` per sealed turn, FULL frozen string +
  index-locator alternative; elasticity untouched (pass-through verified)
- lane unification: `compile_active_context` becomes a block PRODUCER feeding the same
  assembler; graph-only blocks (active-work/receipt) join the TAIL; the adjacency
  conversation rendering is retired in favour of spine + paired reserve
- degradation latch (R7) — acknowledged stateful controller change, with restart story
Exit gates: full suite green; NEW lane-parity test (same session state → byte-identical
prompt with and without an active graph, modulo graph-only tail blocks); reserve-pairing test.

### P5 — Byte gate (the mechanism verdict)
Instrumented probe (existing harness + system-message assertions from P3), scripted s2-like
session, spine on vs off.
Pre-registered success: cross-turn prefix survival median 39–44% → **≥80%**; same-turn ≥96%
maintained. Failure = stop and diagnose; no quality runs until the mechanism is proven —
end-to-end fresh numbers are behavior-confounded and cannot arbitrate this.

### P6 — Quality gate
s2/s5 × n=2 spine-on vs spine-off (s5 = the reordering canary), plus one 50-turn accumulation
scenario (s7) for the long-horizon shape, plus s6 real-memory (recall surfaces intact).
Pre-registered: all pass; fresh/turn and cost deltas reported with the meter; liveness fields
(memory_mode/episodes/recalls) mandatory.

### P7 — Subsumption cleanup (negative diff)
conversation window → paired reserve only · cache_manifest region deleted (digest replaces the
preview line, R3/R4) · `+N earlier`/`…older` counters to TAIL · admission-journal region-name
mapping for analytics continuity.

### P8 — Generational compaction (deferred until spine length actually bites)
Trigger: spine > budget (6–8k tokens). Oldest generation → ONE frozen epoch entry, rendered
once. Closed-topic entries compact before budget forces it (digests carry topic id).
Deferred safely: sessions under ~30 turns never hit the trigger; P5/P6 do not depend on it.

### P9 — Endgame: the numbers that started this
- multi-turn matrix rerun (3 arms × 8 scenarios) with spine + T4: target = cost parity or
  better vs Kimi at r=2% (Kimi 8-scenario total $0.239; slice was $0.373) while keeping the
  flat-peak curve (s7: 13k flat vs 50k/93k)
- ContextBench 5-task spot-check (single-turn: expect no change; verify, don't assume)
- refresh `cost-thesis-pricing-regime` memory + the DeepSeek材料 (task #15) with post-spine
  numbers; the r-sensitivity table regenerated from real ledgers

## Risk register (from the review, ranked)

1. **Head instability nullifies everything** (R6) — mitigated by making P3 a hard prerequisite
   with its own byte assertion, not a cleanup item.
2. **Two renderers drift** (R3) — one renderer, byte-parity test in CI.
3. **Digest lossiness harms deixis/correction-following** (moat lens) — paired reserve kept
   (R8); s5 canary + s6 real-memory in P6.
4. **Degradation oscillation** (R7) — latch + oscillation probe.
5. **Segment/multi-workspace spine holes** (R5) — explicit design decision in P1; byte-identical
   resume scoped honestly until the carrier lands.

## Measurement discipline (standing rules for every phase)

n=2 minimum before any verdict; byte gates before behavior gates; pre-registered success
criteria written in this file before the run; failed configs get failure-mode analysis, not
retries-until-green; every result row carries the liveness fields. The 13-trap catalogue
(apples-to-apples-parity-traps) applies to every A/B in this program.
