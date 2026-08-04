# Session Spine — append-only frozen turn digests in the seed

Status: DRAFT (recon in flight; adversarial review pending). Owner: TT-Wang. 2026-08-04.

## Why (measured, not conjectured)

Provider prefix caching bills everything after the first changed byte. Two instrumented
byte-level diagnoses (s2, consecutive prepared requests, char-proxy) established:

| pair type | prefix survival (median) | dominant break |
|---|---|---|
| within-turn, control | 93.9–95.7% | edited OPEN FILES blocks re-rendered mid-turn |
| within-turn, freeze+m1 | **96.7%** (best measured) | residual per-step appenders (findings) |
| **cross-turn, every config** | **39–44%** | the seed head: whichever per-turn-mutating region leads |

Cross-turn is the expensive one: each turn's first call repays most of the ~11k-token seed at the
cache-miss rate. Four ordering interventions (cache/v2/v3/m1) all failed to move it, because the
seed midsection contains five-plus regions that legitimately change every turn (conversation window
slides, manifest appends, progress/world update, worktree refreshes). Ordering relocates the first
break; it cannot remove the mutation. Meanwhile wholesale reordering destabilized execution
(s5 failed under v1, v2, and v3 — three independent max_steps spins vs a passing control).

Root cause, stated structurally: **a prefix cache rewards append-only byte streams; the current
seed is a re-rendered projection of mutable state.** A projection of changing state cannot be
byte-identical to its previous rendering. Transcript agents get 99% hit rates for free because
their data structure IS an append-only log (with the costs we measure elsewhere: unbounded growth,
93k-token peaks by turn 50, stale file views).

## The idea

Make the seed's historical midsection a real log:

```
[system prompt]                          frozen per session
[SESSION HEAD]  skills / memory / objective / constraints / corrections
                — revision-bound; byte-stable most turns
[SESSION SPINE] turn-1 digest ✦ turn-2 digest ✦ … ✦ turn-(k-1) digest
                — each digest rendered ONCE at seal, stored as FROZEN BYTES,
                  appended verbatim forever after; old entries never re-rendered
[VOLATILE TAIL] open_files (turn-start snapshot) · worktree · findings · intent ·
                turn_contract  — the only rebuilt part, small
[CURRENT REQUEST] / [NOW]                already last, outside the fence
[trajectory]                             append-only by construction
```

The spine entry is created exactly once, inside the seal path, from the same material the seal
already freezes (the EpisodeSink record / local seal artifact). Because the bytes are stored, not
re-projected, turn k+1's request shares its entire head+spine prefix with turn k's last request
except the one appended entry.

Digest content per turn: the user's ask VERBATIM (the P0.3 reserve contract — user-authority
wording is never paraphrased; verbatim text is naturally frozen bytes), outcome, durable deltas
(files touched, key findings), and the history locator
(`read_file("@sliceagent/history/turn-N.md")`) so full recall stays one call away. Size target
≤ ~250 tokens for typical asks; a giant pasted ask stores verbatim up to the reserve budget and
degrades exactly as the reserve degrades today (oldest first, locator fallback).

## What the spine subsumes vs keeps

Subsumed (their information moves into digests): the sliding conversation window (except the
verbatim reserve for the most recent exchange), cache_manifest's per-turn locator lines,
most of progress.

Kept: the verbatim user reserve (a USER-authority surface — the spine digest is agent-authored and
must not replace user wording); world (task-scoped model the agent actively edits); everything in
the volatile tail.

## Boundedness (the moat constraint)

The spine grows ~0.15–0.25k tokens/turn — far below transcript growth (~1–2k/turn measured), but
not zero. Reconciliation with the bounded-peak thesis: **generational compaction**. When the spine
exceeds a budget (e.g. 6–8k tokens), the oldest generation [turns 1..j] is compacted into ONE
frozen epoch-summary entry (rendered once, then immutable like any other entry). Cost model: one
deliberate prefix break per epoch, amortized over the epoch's turns — versus today's break every
turn. Peak stays bounded; cache cost becomes amortized-small; recall of compacted turns stays
available via history locators.

## Validation plan (pre-registered)

1. **Byte probe** (existing instrumented harness): cross-turn prefix survival, control vs spine.
   Success: median 40% → ≥80%. This is the mechanism gate — it cannot be faked by behavior change.
2. **Quality gate**: s2/s5 × n=2, spine on vs off, memory null. All pass required. s5 is the
   canary: it failed under every reordering; the spine does NOT reorder the head (skills/memory
   lead as today) and keeps the ask cluster last, so the s5 risk is lower but must be proven.
3. **Meter**: fresh/turn and cost vs control; expected fresh reduction ≈ the cross-turn seed
   repayment (~8–9k/turn of ~11k).
4. Liveness fields carried as always (memory_mode / episodes / recalls).

## Phases

- **A. Spine store**: at seal, render + freeze the digest string; carry the append-only list in
  Slice state (and persist via the existing seal artifact so resume rebuilds the same bytes).
- **B. Seed integration**: new SESSION SPINE region emitting the frozen concatenation verbatim;
  elasticity must treat it as pass-through (drop-whole-entry only from the OLDEST end, never
  reflow) — seam confirmed by recon.
- **C. Subsumption**: conversation window → verbatim reserve only; manifest lines → digest field.
- **D. Compaction**: generational epoch summary (flag-gated, later).

## Coverage map — surfaces the spine MUST handle (added after the reserve/two-lane review)

1. **Verbatim user reserve (P0.3) is a hard constraint**: digests EMBED the verbatim user ask
   (frozen bytes are naturally verbatim-compatible); an agent-authored paraphrase would silently
   replace user-authority wording for turns 2..12. Reserve degrade-oldest-first maps to
   drop-oldest-spine-entry-first.
2. **Two lanes**: legacy `build_context_blocks` vs graph `compile_active_context`/`_adjacency_blocks`
   (seed.py:589) — the adjacency lane renders its OWN conversation with the mirrored reserve band
   (context_compiler.py:249). Every layout flag this sprint touched only the legacy lane; benchmarks
   run legacy, production interactive often runs graph. The spine must enter at a seam BOTH lanes
   share, or be wired into both with a lane-parity test (the sticky-plan-mode bug class).
3. Subagent scoped turns: spine records parent logical turns only; delegation arrives via the parent
   digest + SubagentFS locators.
4. Workspace handoff segments: digests mark segment_outcome (workspace_transition vs delivery).
5. Aborted/interrupted turns: recorded with outcome=aborted — no information holes.
6. Topic switches: digests carry the topic id.
7. Resume: digest bytes persist with the seal artifact; a restarted session must reproduce
   byte-identical spine content.
8. Admission/metrics downstream: the new region name enters the admission journal; analytics
   mapping updated.

## Open questions for recon/review

- Exact seal-path hook point and what the sealed record already contains (avoid double-rendering).
- Elasticity pass-through semantics: can a block opt out of alternatives entirely today?
- Resume: are sealed bytes recoverable so a restarted session reproduces identical spine bytes?
- Interaction with T4 aliases (both append-only — expected compatible, must verify byte-level).
- Digest authorship: purely mechanical rendering (no LLM call) to keep seal cheap and deterministic.
