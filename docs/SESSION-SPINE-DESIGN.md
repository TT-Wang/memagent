# Session Spine — append-only frozen turn digests in the seed

Status: **v2 — verdict folded**. Supersedes the v1 DRAFT of 2026-08-04. Adversarial review:
GO-WITH-CHANGES (`SESSION-SPINE-REVIEW.md`); every required change R1–R12 is folded below or
listed under Explicitly deferred with the review's rationale. Phases live in
`SESSION-SPINE-ROADMAP.md` (P0–P9) — this doc is the architecture those phases implement and
does not duplicate them. Recon: `SESSION-SPINE-MAP.md`; where the review overrides the MAP,
this doc says so at the point of conflict (see also Consistency notes at the end).
Owner: TT-Wang. 2026-08-04.

## Why (measured, not conjectured)

Provider prefix caching bills everything after the first changed byte. Two instrumented
byte-level diagnoses (s2, consecutive prepared requests, char-proxy) established:

| pair type | prefix survival (median) | dominant break |
|---|---|---|
| within-turn, control | 93.9–95.7% | edited OPEN FILES blocks re-rendered mid-turn |
| within-turn, freeze+m1 | **96.7%** (best measured) | residual per-step appenders (findings) |
| **cross-turn, every config** | **39–44%** | the seed head: whichever per-turn-mutating region leads |

The 39–44% row is the *harness* baseline. Production is strictly worse: the system prefix there
carries a live git branch/dirty/HEAD line (sensory_cortex.py:280-287 via seed.py:366-372), a
re-walked repo map, and the plan-mode overlay — each a per-turn byte break upstream of
everything (R6).

Cross-turn is the expensive one: each turn's first call repays most of the ~11k-token seed at the
cache-miss rate. Four ordering interventions (cache/v2/v3/m1) all failed to move it, because the
seed midsection contains five-plus regions that legitimately change every turn (conversation window
slides, manifest appends, progress/world update, worktree refreshes). Ordering relocates the first
break; it cannot remove the mutation. Meanwhile wholesale reordering destabilized execution
(s5 failed under v1, v2, and v3 — three independent max_steps spins vs a passing control). Those
four dead layout branches have since been deleted outright (roadmap P0, `904914f`).

Root cause, stated structurally: **a prefix cache rewards append-only byte streams; the current
seed is a re-rendered projection of mutable state.** A projection of changing state cannot be
byte-identical to its previous rendering. Transcript agents get 99% hit rates for free because
their data structure IS an append-only log (with the costs we measure elsewhere: unbounded growth,
93k-token peaks by turn 50, stale file views).

## The architecture — one lane, every block frozen or live

North star (ROADMAP): **one lane, one schema.** The prompt is built by ONE assembler from blocks
of exactly two kinds:

- **frozen** — sealed bytes, rendered once at seal, ordered by seal order, never re-rendered
- **live** — rebuilt this turn, small, ordered by a fixed tail layout

```
[system]        epoch-pinned (R6): computed once per session/epoch; no live git line,
                no plan-mode overlay, repo map pinned per epoch
[SESSION HEAD]  durable knowledge: skills / memory snapshot (per topic/session, R6) /
                objective / constraints / corrections — byte-stable within an epoch
[SESSION SPINE] segment-1 digest ✦ segment-2 digest ✦ … ✦ segment-(k-1) digest
                — one entry per sealed segment (R5), each rendered ONCE inside the seal,
                  stored as FROZEN BYTES, appended verbatim forever; never re-rendered
[VOLATILE TAIL] paired verbatim reserve (R8) · open_files (turn-start snapshot) ·
                worktree + git line (R6) · findings · progress · world · live truth ·
                counters · receipt · ACTIVE WORK (R11) · intent · turn_contract
                — the only rebuilt part, small; [CURRENT REQUEST]/[NOW] last
[trajectory]    append-only by construction
```

Non-negotiable invariants, in priority order (ROADMAP):
1. **User authority**: verbatim (post-redaction, R2) user wording is never paraphrased; the
   paired verbatim reserve survives (R8) and the spine subsumes only turns older than it.
2. **Bounded peak**: spine + compaction keeps per-call peak history-bounded (the moat).
3. **Byte honesty**: frozen means frozen — a renderer upgrade must not rewrite history (R1);
   every gate is pre-registered; the byte gate precedes the quality gate.
4. **One path**: both former lanes consume identical frozen entries through one assembler;
   a lane-parity test guards the seam until the legacy lane is deleted.

**Lane decision (R11, decided — not deferred).** v1's coverage item 2 treated the legacy
`build_context_blocks` vs graph `compile_active_context`/`_adjacency_blocks` (seed.py:589) split
as a surface to handle; the North star deletes the split. Until the legacy lane is gone: the
spine renders on EVERY turn in BOTH lanes, never gated on `needs_history` — per-turn presence
flipping of pre-spine regions is itself a byte break (context_compiler.py:334-349) — and its
region name is added to `compile_active_context`'s always-selected set. ACTIVE WORK (order=-1,
slot 0, mutates per step, embeds graph.revision) is a live control surface: by this doc's own
taxonomy it moves below the spine into the volatile tail. The graph lane's resident cost is
counted in the meter gate. Lane parity is guarded by a byte-identity test (roadmap P4) until
`compile_active_context` becomes a block producer feeding the one assembler.

Because the bytes are stored, not re-projected, turn k+1's request shares its entire
head+spine prefix with turn k's last request except the one appended entry.

## Write side — the digest is sealed bytes

Digest authorship is purely mechanical — no LLM call — to keep seal cheap and deterministic
(v1 open question, confirmed correct by the review's verdict).

### R1 — Durable byte home: a field of `safe_record`, rendered inside `_seal_locked`

The digest is rendered after the indeterminate status upgrade (runtime_persistence.py:756-793)
and embedded as a field of `safe_record` before the Artifact is constructed at
runtime_persistence.py:832. The bytes therefore ride the existing 3-stage
`prepare_seal → artifacts.put → checkpoint CAS` protocol and its journal replay **unchanged**.

**This overrides the MAP.** MAP §2's "append inside the same atomic `coordinator.seal`" is
unimplementable: the protocol has no 4th stage or replay branch (persistence.py:1265-1287), and
per-(workspace, task) checkpoint state cannot hold a cross-topic session spine (cli.py:1661,
1677). The review wins; the safe_record field is the seam.

**Resume** = scan `kind=='turn'` artifacts for the session, order by `artifact_order_key`
(`order_ns` gives resume ordering for free), concatenate the **stored digest strings verbatim —
never re-render frozen entries**: a renderer upgrade between save and load would silently rewrite
history bytes (invariant 3). The subagent filter is mandatory: children never call
`LocalTurnStore.seal`, but recovery mints `subagent-*` artifacts into the same store
(persistence.py:1336). v1 Phase A's "carry the append-only list in Slice state" is restated:
Slice state is at most a **cache of the scan**, never the source of truth.

**Live-session return channel** = the existing post-seal artifact re-load (cli.py:1667-1670) —
single projection, the typed-return-lane pattern. There is no second pre-seal render in cli:
it would be pre-redaction and pre-status-upgrade.

**Idempotency**: entries key on `artifact_id`; the `_pending_seal_records` retry path
(cli.py:1362, 1571) can offer the same record twice, but in-process double-seal is structurally
prevented (review verdict), and L7 determinism (below) keeps recovery replay idempotent —
a one-byte render difference would convert replay into ArtifactConflictError
(persistence.py:431, 592-593).

### R2 — Redaction contract: post-redaction bytes; "verbatim" means never-paraphrased, not never-redacted

The digest embeds POST-redaction bytes. The journal header request is already
`redact_text(..., preserve_length=True)` (runtime_persistence.py:499) and the artifact brief
carries it (runtime_persistence.py:841). Rendering from the live verbatim text (or from
`EpisodeSink._flush`'s unredacted buffer) would write pasted secrets to disk and re-inject them
into every future seed. One redaction dialect only: `preserve_length=True` — not the mirror's
plain dialect at hippocampus.py:336-338. Test: seal a turn whose ask contains a known secret
pattern; assert spine bytes and artifact agree and contain no secret.

### R3 — One renderer, journal-derivable, serving both seal and crash recovery

v1's coverage claim 5 ("aborted/interrupted turns recorded — no information holes") was false
for SIGKILL as written: recovery materializes "interrupted" artifacts from journal_events +
receipt only (persistence.py:1338-1366), with no access to the dead process's
`_pending_seal_records`. Therefore the digest schema is constrained to be **derivable from
journal-only data** (header.user_request + events + receipt), and ONE renderer serves both call
sites — seal and `coordinator.recover` — with a byte-parity test between them. Two renderers
would be the exact path-asymmetric-wiring bug class the MAP flags for the read side but not its
own write side.

This forces dropping the pagetable/JSONL preview line as a digest input: it does not exist at
digest-render time (`append_episode` runs after seal, cli.py:1690 vs 1638), it is
best-effort-swallowed (hippocampus.py:373-376), and it never exists in memory-null — this
design's own gate config. Manifest subsumption is achieved by the digest **replacing** the
preview line, not embedding it.

Degenerate cases, specced in the schema:
- **abort-during-prep** (synthetic fallback record, cli.py:1573-1576): renders from
  header + receipt only.
- **`waiting_peer`** entries: marked open/parked, never presented as concluded.
- **aborted/interrupted**: `outcome=aborted`, rendered by the same journal-only renderer.

### R4 — Frozen locators cite `artifact_id`, never positional turn-N

v1's locator (`read_file("@sliceagent/history/turn-N.md")`) resolves `_current()[N-1]` filtered
to the CURRENT session_id (contextfs.py:736-740, 811-816): after restart every frozen locator
dangles; one unreadable artifact silently shifts all later frozen locators onto the wrong turn;
and three numbering schemes already diverge (artifact position vs `EpisodeSink._turn` vs
`target.turns`). The machine locator is the immutable `artifact_id`, resolvable in any session
via `sessions/<key>/<artifact_id>.md`; a human-readable turn number is display text only, never
load-bearing.

### R10 — Digest content: provenance-grade facts only; determinism law L7

Digest schema — a pure function of (redacted journal header, journal events, receipt,
`order_ns`, topic id, segment metadata per R5). Fields:

- the user's ask, post-redaction VERBATIM (the P0.3 reserve contract — user-authority wording
  is never paraphrased; verbatim text is naturally frozen bytes)
- outcome/status (incl. `segment_outcome`, R5)
- files touched
- `artifact_id` locator (R4)
- topic id + segment ids
- an epistemic header: **"verbatim claim; not world evidence"**

Banned: current-truth claims — v1's "key findings" is out. Frozen semantic claims reproduce the
stale-transcript pathology in miniature and conflict with the curated volatile FINDINGS region.
Corrections are appended entries citing the corrected `artifact_id`, never edits.

**Law L7**: the digest is a pure function of already-durable inputs — no clock
(Artifact.timestamp is `now()` at seal), no counters, no environment. Test: render-twice,
assert byte-equal, at both call sites (seal and recovery).

Size target ≤ ~250 tokens for typical asks. A giant pasted ask degrades to a bounded verbatim
HEAD + locator with a **loud** truncation marker — never locator-only, and never a silent cap
(the recall-ring-truncation confab shipped twice via silent caps) (R8 corollary).

## Read side — one assembler

### Block shape

One `alternative_group` per sealed segment, following the `_one_adjacency` precedent
(`group = f"active-adjacency:{age}"`, context_compiler.py:159-251): FULL frozen string +
index-locator alternative. Entries are **non-mandatory** — mandatory bytes have no degradation
escape valve; an unboundedly grown spine would hard-fail with ContextUnfitError (MAP §4.4).
Slot/order: dedicated early slot, monotonically increasing `order` per entry;
`render_context_selection` (regions.py:1299-1316) concatenates deterministically by
`(slot, order, block_id)`, so append-only order = append-only bytes — provided every earlier
slot is also byte-stable (that is what R6 buys).

### R7 — Degradation: whole-spine → index locator, with a per-turn latch

v1 (drop-oldest-spine-entry-first) and MAP:82 (truncate newest-first / latch / drop-whole) were
in direct contradiction, and **both partial policies lose**: oldest-first rewrites front bytes
(cache-worst); newest-first evicts the most relevant entries and keeps turn-1 resident,
violating bound-is-relevance-not-size (gotten wrong twice already). Adjudicated (review):

- Under pressure the spine degrades **whole-spine → single index locator** — the existing
  drop-whole FULL+LOCATOR pattern, MAP §4.3's own third option. No relevance inversion, no
  front-byte rewrite in normal operation; the recent context lives in the paired reserve (R8)
  anyway.
- A **per-turn latch**: once degraded this turn, stays degraded — the stateless per-call
  controller at context.py:267-283 currently springs entries back mid-turn. Stated honestly:
  the latch IS a stateful controller change with its own restart story to spec (this
  supersedes MAP's "elasticity needs zero controller changes").
- The spine has **priority above every volatile-tail region**: the tail degrades first.
- Relevance answer for CORE-DESIGN.md:155-159's region-legitimacy test: entries whose topic is
  closed **compact earlier than budget** (digests carry the topic id) — see Boundedness.
- An oscillation probe under fluctuating capacity is part of the byte gate.

### R8 — The paired verbatim reserve stays; the spine boundary is the reserve floor

The reserve holds **paired** exchanges precisely because deixis resolves against ASSISTANT text
("go with your recommendation" at turn 8 against options enumerated in turn 5's reply;
regions.py:54-63, context_compiler.py:226-228), and the locator fallback is a measured-dead
channel (~0 recall on coding turns, context_compiler.py:153-155). The spine subsumes only turns
OLDER than the reserve. Cache wins live in turns 13+, where transcript growth is anyway.
The reserve stays in the volatile tail per today's contract; reserve degrade semantics are
unchanged by the spine.

### What the spine subsumes vs keeps (amended per R3/R4/R8)

Subsumed (their information moves into digests): the sliding conversation window **for turns
older than the paired reserve**; cache_manifest's per-turn locator lines (the digest *replaces*
the preview line, R3); most of progress (origination only — the coalesced current view stays in
the tail, MAP §3).

Kept: the paired verbatim reserve (a USER-authority surface — the spine digest is agent-authored
and must not replace user wording, and deixis needs the assistant half, R8); world (task-scoped
model the agent actively edits); everything in the volatile tail.

### The volatile tail, enumerated exhaustively

Paired verbatim reserve · current request · progress · world · live truth · git line (R6) ·
"…older" / "+N earlier" counters (until P7 deletes them) · receipt block · ACTIVE WORK (R11) ·
open_files turn-start snapshot · worktree. Invariant, enforced by grep audit: **no relative-age
or count text at or before the spine** — changing-count disease inside a stable prefix is a
byte break.

## Segment model (R5 — DECIDED)

One logical turn can seal multiple segments into TWO stores across a workspace handoff
(cli.py:1581-1588, 2350, 2607), so neither disk contains the whole logical turn; v1's coverage
claim 7 (byte-identical resume) was false as written. Decision, recorded here as DECIDED:

- **Spine entries are segment-scoped.** Seals happen per segment, so the entry is the unit that
  actually gets sealed — one entry per sealed segment, each carrying `segment_outcome`
  (`workspace_transition` vs `delivery` vs `aborted`).
- **The repeated ask is deduped by reference.** When one logical turn spans segments, the later
  segment's digest references the first segment's entry by `artifact_id` instead of re-embedding
  the verbatim ask. No paraphrase is involved (invariant 1 holds); the verbatim bytes live once,
  in the first segment's entry.
- **The cross-workspace digest carrier is DEFERRED.** The `workspace_continue` admission record
  (cli.py:1499-1508) is the natural vehicle, but it does not land now. Until it does,
  **byte-identical resume is honestly scoped to single-workspace sessions** — the resume gate
  and any public claim carry that scoping explicitly.

Ordering is `order_ns` / `artifact_order_key` (R1). The `_pending_seal_records.clear()`
interaction is documented as part of the write-side implementation (roadmap P2, checklist item
5). Aborted and interrupted segments seal like any other (`outcome=aborted`, R3's journal-only
renderer) — no information holes. Topic switches: every digest carries the topic id.

## Head stability prerequisites (R6)

The spine never gets a warm prefix unless everything before it is byte-stable. Two live
falsifiers of v1's layout claim ("frozen per session / byte-stable most turns"):

1. The system prefix is rebuilt per segment with live git bytes (branch/dirty/HEAD line,
   sensory_cortex.py:280-287 via seed.py:366-372), a re-walked repo map, and the plan-mode
   overlay — any commit or edit kills every downstream byte. Production is strictly worse than
   the 39–44% harness baseline.
2. `lessons_memo` is reconstructed every `make_build_slice` call keyed on the NEW request
   (seed.py:489-498) — the memory region's bytes change every turn while pinned pre-spine.

Fixes (roadmap P3 — a hard prerequisite that lands BEFORE the spine region, otherwise the probe
gate measures nothing):

- system prefix computed **once per session/epoch** (epoch-pinned);
- the git line moves to the TAIL (`git_worktree_state` already covers live git);
- the repo map pinned per epoch;
- memory recall snapshotted per topic/session — or relocated behind the spine into the tail.

The byte probe gains system-message and memory-region byte-diff assertions (see Validation).

## Boundedness & compaction (the moat constraint)

The spine grows ~0.15–0.25k tokens/turn — far below transcript growth (~1–2k/turn measured),
but not zero. Reconciliation with the bounded-peak thesis: **generational compaction**. When the
spine exceeds a budget B (6–8k tokens), the oldest generation [turns 1..j] is compacted into ONE
frozen epoch-summary entry (rendered once, then immutable like any other entry). Cost model: one
deliberate prefix break per epoch, amortized over the epoch's turns — versus today's break every
turn. Recall of compacted turns stays available via `artifact_id` locators.

**The peak math, stated (R9).** As v1 specified it, resident spine ≈ B + (N/35)·0.4k — linear
in session length, because epoch summaries accumulated with no generation-2 policy; and the seed
at budget is +3–5k over today's flat ~11k. Cost always wins; **peak** is where the moat's
headline metric breaks. Therefore:

- **Epoch summaries count against the same budget B.** They are not free residents.
- **Epoch summaries are purely mechanical**: turn-id range + topic ids + an on-disk epoch-index
  locator — zero semantic summarization. This simultaneously rescues CORE-DESIGN.md:90-92's
  "no routine summarization" claim (the MAP's §4.5 consistency requirement).
- **Generation-2 policy exists**: gen-2 compaction, or hard-cap collapse to the index locator,
  bounds resident spine. Resident spine is capped at B, full stop.
- **The public claim is restated**: per-call peak **ramps to head+B, then holds** — not "flat
  from turn 1". The README/benchmark framing is re-baselined before the meter gate is declared
  green.

**Closed-topic entries compact early (from R7).** Entries whose topic is closed compact before
budget forces it — digests carry the topic id precisely so relevance, not arrival order, decides
what stays resident (bound-is-relevance-not-size).

**Epoch triggers beyond size** (MAP §4.2): the three out-of-band ring rewrites — workspace
rebase (session.py:297-299), restart hydration (cli.py:613-643), task reset — map onto **new
epochs**, never edits to frozen bytes.

## Coverage map — surfaces the spine MUST handle (v1's 8, as amended by the review)

1. **Verbatim user reserve (P0.3) is a hard constraint** — amended by R2 and R8: the digest
   embeds the POST-redaction verbatim ask ("verbatim" = never-paraphrased, not never-redacted);
   the paired reserve is KEPT in the tail and the spine subsumes only turns older than it.
   v1's "reserve degrade maps to drop-oldest-spine-entry-first" is superseded by R7
   (whole-spine → index locator + latch).
2. **Lanes** — superseded by the one-lane North star (R11 decided): spine present every turn in
   both lanes until the legacy lane is deleted; lane-parity byte test guards the seam
   (the sticky-plan-mode bug class).
3. **Subagent scoped turns**: spine records parent logical turns only; delegation arrives via
   the parent digest + SubagentFS locators. The resume scan MUST filter `subagent-*` artifacts
   (recovery mints them into the same store, persistence.py:1336).
4. **Workspace handoff segments**: the full R5 segment model — segment-scoped entries,
   `segment_outcome` ∈ {workspace_transition, delivery, aborted}, ask deduped by `artifact_id`
   reference, cross-workspace carrier deferred.
5. **Aborted/interrupted turns**: `outcome=aborted` — and the claim now survives SIGKILL,
   because the ONE renderer is journal-derivable (R3); crash recovery renders the same bytes
   from journal_events + receipt.
6. **Topic switches**: digests carry the topic id; closed topics compact early (R7).
7. **Resume**: digest bytes persist inside the sealed artifact (`safe_record` field, R1);
   a restarted session reproduces byte-identical spine content by verbatim concatenation of
   stored strings — **scoped to single-workspace sessions until the workspace_continue carrier
   lands (R5)**.
8. **Admission/metrics downstream**: the new region name enters the admission journal;
   analytics mapping updated; the graph lane's resident cost is counted in the meter (R11).

## Validation plan (pre-registered; byte gate BEFORE quality gate)

The order is an invariant (ROADMAP #3): no quality runs until the mechanism is proven —
end-to-end fresh numbers are behavior-confounded and cannot arbitrate the mechanism.

1. **Byte probe** (existing instrumented harness, prefix_probe LCP): cross-turn prefix
   survival, control vs spine. Success: median 39–44% → **≥80%**; same-turn ≥96% maintained.
   This gate cannot be gamed by behavior change (review verdict). Added assertions:
   - **system-message and memory-region byte-diff** across consecutive turns of a scripted
     session, edits allowed, no topic switch (R6);
   - **oscillation probe** under fluctuating capacity — the latch must hold (R7).
2. **Write-side mechanical tests** (roadmap P2 exit gates, no LLM):
   - L7 render-twice determinism at both call sites (seal and recovery) (R10);
   - **byte-parity: seal-path render == journal-only recovery render**, per turn, on a
     scripted session (R3);
   - **redaction test**: seeded secret in the ask → absent from spine bytes and artifact alike
     (R2);
   - **resume byte-identity** test that upgrades the renderer between save and load — frozen
     entries must not change (R1);
   - workspace-handoff resume test, or the documented single-workspace scoping (R5).
3. **Quality gate** (R12 — v1's s2/s5 × n=2 was under-scoped; it detects execution
   destabilization, not the deixis/correction/tone regression class R8 protects against, which
   was historically found only by usersim leading-prompt probes). Gate = the **8-scenario
   matrix** + a **conversational probe** (existing usersim + evals/convo_h2h.py) exercising
   turn-5-referent deixis, ordinal selection, and correction-following, spine on/off. s5 stays
   the reordering canary; roadmap P6 adds the s7 50-turn accumulation scenario and s6
   real-memory.
4. **Meter**: fresh/turn and cost vs control; expected fresh reduction ≈ the cross-turn seed
   repayment (~8–9k/turn of ~11k); graph-lane resident cost counted (R11); the peak claim
   re-baselined per R9 before the meter gate is declared green. Liveness fields carried as
   always (memory_mode / episodes / recalls).

## Explicitly deferred (from the review, with its rationale)

- **Epoch compaction implementation** (roadmap P8) — safe because R9 fixes the *policy and
  claim* now (mechanical summary format, budget accounting, gen-2/hard-cap are specified above);
  sessions under ~30–40 turns never hit the trigger. What was not safe — shipping the moat
  claim unbaselined — is pulled forward into R9.
- **Manifest/window subsumption** (roadmap P7) — safe because R3/R4 already make the digest
  self-contained; until it lands the manifest is merely redundant, not wrong. The "+N earlier"
  counter dies with P7; until then it lives in the tail.
- **Cross-workspace digest carrier** (`workspace_continue`, cli.py:1499-1508) — deferred per
  the R5 decision; byte-identical resume is scoped to single-workspace sessions until it lands.
- **T4 alias byte interaction** (MAP Q7) — both structures are append-only; byte-level
  verification belongs in the Phase B probe run, and a failure there degrades cache hit rate,
  not correctness.
- **`steps[].slice` artifact slimming** (MAP Q8) — pure storage-weight optimization behind a
  consumer audit; no coupling to spine correctness.
- **The pre-existing three-way turn-numbering divergence** (artifact position vs
  `EpisodeSink._turn` vs `target.turns`; a live wrong-recall risk in today's manifest) — safe
  to decouple *only because* R4 removes positional numbers from frozen bytes. It is its own
  task, off the spine's critical path.
- **Kimi-ceiling gap quantification** (MAP Q9) — reporting framing, not mechanism; needed
  before publishing results, not before write-side code.

## Phases

See `SESSION-SPINE-ROADMAP.md`. In this doc's terms: P0 (debt deletion) is done (`904914f`);
this doc is P1's deliverable; the Write side is P2; Head stability prerequisites are P3 and land
before the Read side (P4); the byte gate (P5) precedes the quality gate (P6); subsumption
cleanup is P7; compaction is P8; the endgame numbers are P9. The review's amended Phase A/B
checklist (`SESSION-SPINE-REVIEW.md` §3) is the implementation-level companion to P2/P4.

## Consistency notes — where this doc overrides prior docs

Recorded so no reviewer can cite whichever half supports their preference (the review's
required consistency pass):

- **Seal seam**: MAP §2 "append inside the same atomic `coordinator.seal`" is unimplementable
  (no 4th protocol stage or replay branch, persistence.py:1265-1287; checkpoint state is
  per-(workspace, task), cli.py:1661, 1677). Superseded by R1: digest = `safe_record` field,
  rendered inside `_seal_locked`.
- **Degradation**: v1's drop-oldest-first (DESIGN v1 coverage 1) and MAP:82's inversion options
  are both superseded by R7: whole-spine → index locator + per-turn latch + spine priority
  above the tail.
- **Controller changes**: MAP's "elasticity needs zero controller changes" is superseded — the
  R7 latch IS a stateful controller change, with a restart story to spec.
- **Quality gate**: v1's s2/s5 × n=2 is superseded by R12: 8-scenario matrix + usersim
  deixis/correction probe.
- **Digest inputs**: MAP §2's "render from … + the pagetable preview line" is superseded by
  R3: journal-derivable inputs only; the digest replaces the preview line.
- **Locators**: v1's `history/turn-N.md` positional locator is superseded by R4: `artifact_id`.
