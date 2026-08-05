> **RETIRED (tape graduation, 2026-08-05)** — the spine region + mode described here was deleted in wave 1 (docs/TAPE-GRADUATION.md). The document remains as the evidence chain; the runnable historical arm lives at git tag `lab-2026-08-05`.

# Session Spine — Integration Map

Synthesis of five subsystem reports (seal-path, conversation-ring, manifest-progress, elasticity, peer-precedent) for the append-only frozen-turn-digest design in `docs/SESSION-SPINE-DESIGN.md`.

---

## 1. What exists that the spine can reuse

**Frozen per-turn artifacts (already immutable on disk):**

| Asset | Reuse value | Where |
|---|---|---|
| Sealed turn Artifact (canonical bytes, write-once, content-verified) | The commit anchor: spine entries key on `artifact_id`, order on `meta.order_ns` / `artifact_order_key` | `packages/sliceagent-core/src/sliceagent_core/persistence.py:573-594, 525-545`; produced by `runtime_persistence.py:711-865` |
| `structured_body['markdown']` — `turn_markdown()` rendered ONCE at flush, explicitly Slice-decoupled ("Markov") | Proof the "render once at seal" pattern already works; too big/unbounded to be the digest itself (~2.4 KB median, unbounded in steps, pre-redaction) | `packages/sliceagent-cli/src/sliceagent_cli/hippocampus.py:124-152` |
| `compact_receipt_projection` — constant-size receipt dict | Shape template for the digest's fixed-size metadata half (counts, disposition, status) | `packages/sliceagent-core/src/sliceagent_core/receipts.py:498-541` |
| Episodic JSONL row — one append-only line per turn, FileLock'd, never rewritten | Already the byte-stable substrate the manifest reads from | `hippocampus.py:354-379` |
| Manifest line per old turn — `_pack_thissession_preview` is a deterministic pure function of the immutable row; byte-identical on every later render | Can be frozen verbatim into the digest as its locator line (`read_file("@sliceagent/history/turn-N.md")`) | `packages/sliceagent-core/src/sliceagent_core/pagetable.py:158-192, 224-277`; `regions.py:110-130` |
| `history/turn-N.md` aliases + `evidence/turns/` immutable seals | The recovery target every frozen digest cites | `docs/MEMORY-LAYERS-DESIGN.md:67-72` |

**Mechanisms (proven, reusable):**

- **Frozen-bytes passthrough precedent**: `AGENT_FREEZE_OPEN_FILES` (commit 57d2c8f) — snapshot bytes once, reuse verbatim across within-turn re-projections; commit message already articulates the completeness argument (snapshot + trajectory = complete information). `seed.py:507-525`.
- **Elasticity needs zero controller changes**: `ContextBlock` is frozen, `content: str` passes through verbatim; `mandatory=True` = never-degrade; the ubiquitous FULL+LOCATOR two-alternative pattern is already drop-whole-never-reflow (no builder emits EXCERPT/DIGEST). `context.py:135-173, 251-254; regions.py:1271-1295`.
- **Per-group append pattern**: `_one_adjacency` (`group = f"active-adjacency:{age}"`, full + locator, `context_compiler.py:159-251`) is the exact block shape a spine entry should take.
- **Measurement infra**: `prefix_probe.py` LCP probe (commit 8ccf2be) produced the 39–44% baseline; hardbench already records per-call cached tokens.
- **Peer validation**: Kimi's wire.jsonl is literally an append-only log resent verbatim, earning a measured 99.2% cache-read share over 1,282 calls — the empirical ceiling the spine chases.

**What does NOT exist**: any small, redacted, per-turn digest designed for seed inclusion. Every candidate today is either too heavy (artifact: median 18.2 KB, dominated by per-step `steps[].slice` seed copies), semantically empty (compact receipt), mutable (`consolidate_checkpoint` re-projects live state), or pre-redaction (`turn_markdown`). **The digest is new code.**

---

## 2. The seam

### Write side (seal path) — one choke point, already exists

```
loop.py:2261  TurnEnd (loop does NOT seal)
  → cli.py:1549  _seal_local_turn(stop_reason, ...)      ← host-side coordinator
      episodic.take_last_record()                          (hippocampus.py:168-177)
      sealed_target = deepcopy(target); sealed_target.seal()
      → runtime_persistence.py:711  LocalTurnStore.seal(...)
          builds Artifact + Checkpoint
          coordinator.seal(journal, artifact, checkpoint)  ← ATOMIC COMMIT
      cli.py:1660-1676  re-load committed artifact → install last_receipt
```

**Touch points:**
- **`LocalTurnStore.seal`** (`runtime_persistence.py:711-865`): render the spine digest from the *committed-truth* record (post-redaction, post `indeterminate` upgrade at :758-793) and append it **inside the same atomic `coordinator.seal`**. Idempotent per `artifact_id` (the `_pending_seal_records` retry path at `cli.py:1362, 1571` can offer the same record twice).
- **Digest inputs**: the same `safe_record` + meta the artifact embeds — i.e., render from `title / note / meta.files / outcome / turn_receipt counts` + the pagetable preview line, all through the same `_redact` the artifact goes through. Never from `EpisodeSink._flush`'s unredacted buffer.
- **Freeze timing for the conversation pair**: only after `slice_reducer.py:214-224` fills the assistant slot on final `AssistantText` — the seal already runs after that, so seal-time is correct; just never render the digest earlier.

### Read side (seed / elasticity seam)

- **`compile_active_context`** (`context_compiler.py:289-364`): new spine-entry emitter alongside `_adjacency_blocks` / `_receipt_block` (:360-364) — one `alternative_group` per sealed turn, frozen FULL string + tiny LOCATOR alternative, loaded from the sealed archive, **not** re-rendered.
- **`seed.py build()`** (`seed.py:467-598`): second injection point into `logical_blocks` before SeedPlan construction (:589-598) for the legacy lane. **Both lanes must consume the same frozen entries** (path-asymmetry is a documented recurring bug class).
- **Slot/order**: dedicated early slot, monotonically increasing `order` per entry; `render_context_selection` (`regions.py:1299-1316`) concatenates deterministically by `(slot, order, block_id)`, so append-only order = append-only bytes — *provided every earlier slot is also byte-stable*. Must be coordinated with `AGENT_SEED_LAYOUT` flag re-slotting (`regions.py:1210-1252`).
- **Within-turn**: nothing to change — `build_slice()` runs once per turn (`loop.py:1964`) and `project()` only re-selects, never re-renders (`context.py:333-363`).

---

## 3. What the spine subsumes vs. what must stay

| Region | Verdict | Detail |
|---|---|---|
| **cache_manifest** | **Fully subsumed** | Each line is already a pure function of an immutable JSONL row; freeze it into the turn's digest and stop tail-reading the JSONL per build (`seed.py:543-548` lookup goes away). The `…older` count and the k=50 sliding-window head-drop are the non-append-only parts — they move to the volatile tail or die. |
| **conversation ring (prior completed pairs)** | **Subsumed** | Rows are logically write-once (user at `record_user`, assistant once at final AssistantText). Frozen pair bytes replace re-rendering; the write-time trim (`pfc.py:440`), the `+N earlier turn(s)` counter (`regions.py:187-193`), and in-progress-row exclusion all move out of frozen bytes. **Must stay volatile**: the last 1–2 exchanges verbatim for deictic resolution (recall-ring truncation confab is a measured failure class), the current-request block (already outside the fence, `seed.py:564`), and the in-progress row. |
| **progress** | **Partially — origination only** | Signal *origination* can log into digests, but coalesce/count `x{n}`/recency-reorder/cap-8 (`slice_state.py:102-125`) is an irreducibly mutable current view. At ≤8 lines it's cheap: keep whole in the volatile tail. |
| **world** | **NOT subsumed** | In-place-overwritten key→value store mutable at any step (`slice_reducer.py:417-425`); its value IS its current state. Stays in the volatile tail (optional per-turn deltas in digests for provenance). |
| **Per-turn-changing counters** (`+N earlier`, `…older`) | Move to volatile tail | Both are changing-count disease inside otherwise-stable prefixes. |
| **Live regions** (open files, git probe, focus, reconciliation) | Stay volatile | Already understood as LIVE-freshness; `AGENT_FREEZE_OPEN_FILES` handles within-turn stability separately. |

Resulting seed shape: `[stable head] [spine: frozen digest per sealed turn, append-only] [volatile tail: last verbatim exchange(s), current request, progress, world, live truth, counters]`.

---

## 4. Boundedness design constraint

The spine grows one entry per turn — an unbounded-append structure inside a system whose thesis is **bounded peak**. Reconciliation:

1. **The bound is the digest size, not zero growth.** A ~200–400-byte digest × N turns grows the seed by ~0.1–0.2 KB/turn — vs. the transcript arm's per-turn transcript growth. The moat claim restates as: *peak grows O(turns) with a tiny constant, and marginal cost per turn is nearly all cache-read* (Kimi's economics: 543 M cache-read vs 4.3 M other tokens). Amortized fresh-token cost per turn ≈ digest size + volatile tail, which is the number to gate on.
2. **Epoch compaction as the escape valve.** When the spine crosses a budget, seal an epoch: replace the frozen prefix with one summary entry + re-baseline. This pays exactly one full-prefix cache miss — Kimi's own wholesale compaction (829 k→21 k tokens, messageCount 2186→2) is the reference behavior and shows peers accept this cliff when rare. The three out-of-band ring rewrites (workspace rebase `session.py:297-299`, restart hydration `cli.py:613-643`, task reset) map naturally onto **new epochs**, never edits to frozen bytes.
3. **Shed from the TAIL, never the head.** Current elasticity convention pages the *oldest* entry first (`context_compiler.py:246-250`) — that rewrites front bytes and kills the whole suffix cache. Spine degradation must invert: latch selections, truncate newest-first, or drop the whole spine to its index locator. `test_user_reserve.py`'s ascending-band assertions get redefined accordingly.
4. **Do not rely on `mandatory=True` for the spine.** Mandatory bytes have no degradation escape valve — an unboundedly grown spine would hard-fail with ContextUnfitError. Spine entries should be non-mandatory with a locator alternative, plus the epoch policy above as the real bound.
5. **CORE-DESIGN consistency** (`CORE-DESIGN.md:88-92`): digests are rendered once at seal from already-sealed material — the epoch summary must likewise be a projection over already-frozen digests, never a batch re-summarization of a growing transcript, or the doc contradicts the thesis chapter.

---

## 5. Top 5 risks, ranked

1. **Atomicity/idempotency at seal (correctness).** Seal runs on a deepcopy, publishes only after durable commit, retries the same record via `_pending_seal_records`, and can retroactively upgrade status to `indeterminate` *during* commit (`cli.py:1362, 1571, 1589-1593, 1660-1677`; `runtime_persistence.py:758-793`). A spine append outside the atomic commit, or rendered pre-commit, produces orphaned or wrong-status frozen bytes — permanently, since they're append-only. *Evidence: seal-path risks 1–2.*
2. **Redaction bypass (security).** `turn_markdown` renders from unredacted buffered data; redaction happens later in `LocalTurnStore.seal`/`_clamp_record`. Frozen spine bytes concatenated into *every future seed* would bypass the redact-on-persist boundary unless rendered from the redacted record — and redaction non-idempotence has previously caused hash desync (subagent seal history). *Evidence: seal-path risk 3; memory `subagent-perf-seal`.*
3. **Prefix stability depends on everything upstream (cache efficacy).** Frozen spine bytes only help if every block ordered before them is byte-stable across calls AND turns: FULL↔LOCATOR oscillation under fluctuating capacity (stateless per-call select, `context.py:267-283`), the per-call `prepare` hook splicing live bytes (`loop.py:697`), `AGENT_SEED_LAYOUT` slot reassignment, and the unverified T4 result-alias byte interaction (`SESSION-SPINE-DESIGN.md:101`) can each defeat the append-only property from outside. *Evidence: elasticity risks 1–4, 6; peer-precedent risk 4.*
4. **Segment/epoch semantics vs "one entry per turn" (data model).** Aborted/error/waiting_peer turns seal; workspace transitions seal; one logical turn can produce MULTIPLE segment artifacts (`segment_index`, `cli.py:1581-1588, 2568-2872`); older artifacts on disk lack `order_ns`/`logical_turn_id` (observed schema drift); resumed sessions could theoretically restart turn numbering. A naive one-entry-per-seal spine gets ordering and identity wrong. *Evidence: seal-path risks 4–5; manifest-progress risk 6.*
5. **Deictic/behavioral regression + test blast radius (behavior).** The verbatim ring exists so "yes"/ordinals/corrections resolve against exact prior text; freezing digests without a verbatim volatile tail regresses intent resolution (measured confab class). 16 test files touch the ring; `test_conversation.py`, `test_user_reserve.py`, and 4 adjacency tests pin exact bytes and trim arithmetic and need rewrite — and the layout-experiment history shows seed restructuring can destabilize execution (s5 max_steps spins under v1–v3), so the 8-scenario matrix is a mandatory gate. *Evidence: conversation-ring risks 1, 6–7; seal-path risk 6; peer-precedent (v1–v3 failures).*

---

## 6. Open questions the design doc must answer

1. **Digest content spec**: exactly which fields (title? note one-liner? files? outcome? manifest preview line? locator?), the size cap, and the redaction guarantee — nothing existing has the right shape, so this is a from-scratch schema decision.
2. **Boundedness policy**: hard budget for the frozen concatenation; epoch-compaction trigger, summary-entry format, and expected cadence (with Kimi's one-time-cliff numbers as the cost model); restatement of the bounded-peak invariant now that "bytes resident" and "bytes appended" diverge (what does `USER_RESERVE_TOKENS` govern post-spine?).
3. **Segment model**: one spine entry per sealed segment or per logical turn? How are `workspace_transition`, aborted, and `indeterminate` segments rendered? Ordering key for pre-`order_ns` legacy artifacts.
4. **Epoch triggers beyond size**: are workspace rebase, restart hydration, and task reset each a new epoch, and what does the epoch-boundary entry look like for each?
5. **Degradation policy inversion**: latch vs tail-truncate vs whole-spine-to-locator under pressure; how `test_user_reserve`/adjacency ordering assertions are redefined; non-mandatory-with-locator vs mandatory tradeoff.
6. **Selective-omission behavior**: kernel-graph turns currently render no manifest unless `needs_history` (`seed.py:546-547`) — does the spine always ride, and what does that cost on graph-active turns?
7. **T4 alias interaction**: byte-level verification that alias tables render identically across turns inside/around the frozen region (explicitly flagged unverified).
8. **Consumer audit before slimming**: can `steps[].slice` (the 7.5 KB-per-step seed copy dominating artifact weight) shrink once the spine exists, and which monitor/replay tooling reads it?
9. **Success gate framing**: why ≥80% median prefix survival is the floor vs Kimi's 99.2% transcript baseline — quantify the residual gap (volatile tail + epoch boundaries) so reviewers don't read 80% as underachievement.
10. **Law extension**: formalize the new claim — L4 says identical state → byte-identical context; the spine claims state *growth* → pure byte *append* — as a testable law (L7?) gated by the existing `prefix_probe` LCP infra plus the 8-scenario behavioral matrix.