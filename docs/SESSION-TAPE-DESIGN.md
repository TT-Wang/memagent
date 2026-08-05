# Session Tape — the single append-only stream

Status: **IMPLEMENTED** (typed core, 2026-08-05), owner TT-Wang. Successor to Option B
(`OPENFILES-SUBSUMPTION-DESIGN.md`) and the Session Spine (`SESSION-SPINE-*`).
Implementation: `packages/sliceagent-core/src/sliceagent_core/tape.py` · gates in
`tests/test_session_tape.py`.

**Where the implementation supersedes this draft** (the draft below is kept as the design
rationale; the code + its docstrings are authoritative — review R2-F8):

- **Typed core**: the tape is `list[TapeEntry]` (kind/path/payload/no_nl/post_hash/ref +
  frozen `rendered` bytes). Rendered text is never re-parsed; GC/fold/durability reason over
  the typed fields only.
- **Defer-base-until-edit**: reads never found bases (the draft's first-read base would let
  bloat reads colonize the tape — s10 measured ~360k chars). The first EDIT founds the base
  from its post-state; read-only material lives in the trajectory + hash index.
- **Patches**: TRUE unified diffs from event-time disk snapshots (difflib n=1, a/b labels),
  never argument-shaped. No-trailing-newline files stay byte-exact via the entry's `no_nl`
  flag; rendering annotates instead of lying.
- **Re-base is reactive only**: rendered-size choice per edit (patch block vs base block),
  honesty-net drift, and fold re-anchoring. No count/byte chain triggers.
- **Budget**: 120k chars default (`AGENT_TAPE_BUDGET`), fold-to-0.7× hysteresis (48k + no
  hysteresis thrashed: s2 r3). Fold RE-ANCHORS every file with entries in the folded span to
  its registry content as one fresh base — carried-stale-base + dropped-patches broke
  composition (both external reviews, P1).
- **Digest**: the sealed artifact's spine_digest string is appended VERBATIM (one render,
  seal redaction inherited). The conversation region is retired under the tape; the turn's
  outward answer freezes as a `[reply]` entry capped at 1200 chars (R8 note: deixis beyond
  the cap resolves via the sealed turn artifact).
- **Durability**: an append-only JSONL journal per session (`~/.sliceagent/tape/<session>.jsonl`),
  written at seal; `load_session_tape` replays it (post_hash-verified patch application) and
  compacts once. Crash between artifact commit and journal write loses at most one turn's
  entries; the next seal's honesty net re-anchors loudly. (The draft's `safe_record
  ["tape_entries"]` embedding remains open as a follow-up if artifact-embedded parity is
  wanted.)

## 0. The evidence chain that forces this shape

Every measured fact from the 2026-08-04/05 byte campaign points at one design:

1. **Spine (P5)**: an append-only frozen segment SURVIVES across turns — cp grew +~0.5k/turn
   with the break pinned at the newest entry (the theoretical optimum for a growing frozen
   record). But the ratio capped at 33.6% because ~70% of the request (OPEN FILES/findings
   midsection) sat below it and re-billed every turn.
2. **Locators (Option B, loc r1)**: the midsection is compressible — OPEN FILES went ~20k -> 1.3k
   chars, cross-turn survival jumped to a stable 66–73%, ability and read-discipline both held.
   But economics went NEGATIVE (+22% vs control at DeepSeek prices): the model re-read 41 files
   in 10 turns and paid +28 model calls, because the slice seals away the very history that
   would have made those reads unnecessary.
3. **Kimi audit (same s2 scenario, wire-level)**: the transcript agent read 8 times TOTAL for 40
   edit/write calls — five turns had ZERO reads yet performed 4–6 correct edits each. Its trick
   is not reading: **current file = the base it read once + its own edit stream, both still in
   context**. The transcript's edit log IS its file cache: base billed fresh once then cached
   forever, each edit a few hundred appended bytes.
4. **Geometric constraint**: prefix caching admits only ONE growing region. Stacked append-only
   regions do not compose — any growth above re-bills everything below, byte-identical or not.
   Kimi works because everything interleaves into a single chronological stream.

Conclusion: to get transcript-shaped billing inside a boundable context, the slice needs ONE
chronologically interleaved, append-only, frozen stream — the **session tape** — carrying turn
digests, file bases, and edit patches; with explicit re-base and generational compaction giving
the bound a transcript can never have.

## 1. Prompt shape

```
[system — epoch-pinned, byte-stable]
[TAPE — append-only frozen bytes, the cache-hit zone (chronological interleave):
    (turn t1 digest) (base a.py@h0: full body, once) (patch a.py#1) (turn t2 digest)
    (base b.py@h0) (patch a.py#2) (patch b.py#1) (turn t3 digest) ...]
[TAIL — small volatile zone:
    file index (Option B locator lines: path · lines · CURRENT hash · read call),
    intent family · findings · worktree · current ask last]
[trajectory — within-turn, already append-only (verified: gap==1 on every captured pair)]
```

Per-turn fresh cost target: new patches + one digest + the tail ≈ 1–3k chars, versus 15–25k
today. Cache-hit share approaches the transcript's ~99%.

## 2. Tape entry types (typed, R10 provenance-grade)

All entries are rendered ONCE by a deterministic renderer at the moment they are appended, then
treated as frozen bytes forever (R1/R3 discipline, exactly as the spine's digests are today).
All inputs are post-redaction (R2). Every entry carries a locator to durable truth.

- **`turn` digest** — the existing spine digest, unchanged (render_turn_digest).
- **`base`** — a file's full numbered body at first entry into the working set, tagged
  `base <path> @sha256:<hash12> (<N> lines)`. Appended when the file is first read/opened, or
  at re-base (below). write_file/create = a base (the whole new content is the entry).
- **`patch`** — one successful edit, host-authored from the edit the host itself applied:
  `patch <path> @<pre-hash>-><post-hash>: str_replace old="..." new="..."` (verbatim args,
  redacted, size-capped with a loud truncation marker + artifact locator for the rest).
  The MODEL never generates these; the host knows them exactly. Composition contract for the
  model: current content of `<path>` = latest `base` for that path + every later `patch`, in
  tape order. The TAIL's file index shows the CURRENT hash so the model can verify its
  composition target matches reality before editing.
- **`external`** — the honesty entry: at turn start the host hash-checks tape-tracked files;
  a mismatch (user/other process changed the file) appends
  `external <path> @<old>-><new> — content changed outside this session; re-read before edit`.
  This is what makes base+patch composition SAFE where Kimi merely gambles: the tape never
  silently lies about a file it tracks.

Findings/conversation/knowledge do NOT enter the tape in v1 (attribution discipline: one new
mechanism per experiment; they remain in the tail exactly as today).

## 3. Boundedness: re-base and generational compaction

The tape grows — slower than a transcript (no tool outputs, no reasoning), but it grows. The
bound is explicit and pre-registered, not emergent:

- **Re-base**: when a file's patch chain exceeds `REBASE_PATCH_BYTES` (default 1.5x the file's
  base size) or `REBASE_PATCH_COUNT` (default 12), append a fresh `base` at the tape end; the
  old base+chain become dead bytes that the next compaction removes. Between compactions they
  still cache-hit (frozen), so re-base itself never breaks the prefix.
- **Generational compaction (the P8 design, now with a concrete subject)**: when the tape
  exceeds `TAPE_BUDGET` (shipped default: 120k chars, `AGENT_TAPE_BUDGET`), the oldest generation — dead
  bases/chains, digests of closed topics — collapses into ONE frozen epoch entry holding
  locators to the sealed artifacts. This breaks the prefix ONCE per compaction event, then
  stability resumes. Peak is therefore bounded by head + TAPE_BUDGET + tail.
- **The public claim is restated, not renumbered (R9d)**: per-call peak "ramps to head+B and
  holds, with saw-tooth compaction events" — never "flat from turn 1".

## 4. Durability, recovery, redaction

- Tape entries are fields of the sealed turn artifact (`safe_record["tape_entries"]`), exactly
  like `spine_digest` today: rendered pre-seal from journal-derivable inputs, riding the
  existing 3-stage atomic seal. Resume = scan (`load_session_tape`), same-session scope, same
  honest-empty semantics as the spine on a fresh session id (R5 deferral carries over).
- Crash recovery renders what the journal proves (bases/patches whose edits reached the
  journal); `status=interrupted` digests as today. One renderer per entry type, byte-parity
  gated seal-vs-recovery (the P2 gate pattern, extended per type).
- Patches contain file text -> redact_text before freezing, same dialect as everything else.
  A seeded-secret gate per entry type (the existing P2 redaction gate, extended).

## 5. What this supersedes / absorbs / leaves alone

| Existing piece | Fate |
|---|---|
| SESSION SPINE region | absorbed — digests become one tape entry type; flag retired into the tape flag |
| Option B locator region | absorbed as the TAIL's file index (current-hash line per file) — already built |
| Option B read discipline | replaced by the composition contract (§2 patch entry) — reads become the exception (external/mismatch), not the rule |
| build_artifacts full-body region | retired under the flag (control path untouched) |
| AGENT_FREEZE_OPEN_FILES | obsolete under the flag |
| T4 result alias, projection pin, micro-compact, paired reserve, findings | unchanged |
| Subagents | children keep scoped fresh contexts; no tape inheritance in v1 (spawn snapshot already carries what the child needs) |

Flag: none — the tape is UNCONDITIONAL since graduation wave 2 (the kill switch and the
spine/locators flags all retired; historical arms: git tag `lab-2026-08-05`).

## 6. Pre-registered gates (committed before any run; 13-trap catalogue applies)

Arms: control = `AGENT_SESSION_SPINE=1` (the measured 33.6% config) vs tape. Scenarios: s2
(file-heavy, directly comparable to every prior number) + s7_flagtable_50 (long horizon,
compaction actually triggers). n=2 each. Order: mechanical gates -> byte -> ability -> cost.

1. **Mechanical (free)**: renderer determinism per entry type; append-only invariant (a later
   build may only extend the tape); seal-vs-recovery byte parity; seeded-secret redaction;
   composition oracle (host replays base+patches and byte-compares against disk after every
   sealed turn — a drifted tape is HARNESS INVALID, the run stops).
2. **Byte gate**: s2 cross-turn survival ≥ **85%** median (the tape's own predicted shape;
   69% was reached with the tail alone). Attribution rule as in Option B: breaks must be at
   the tape tip or a compaction event; anything else names the defect.
3. **Ability gate (first among the API gates in verdict order)**: all oracles pass, zero
   max_steps stops, str_replace mismatch rate ≤ control's (composition errors are THE risk;
   Kimi's measured blind-edit style is the existence proof, our `external` entries + current-
   hash index are the safety net it lacks).
4. **Peak gate**: s7 per-call peak ≤ head + TAPE_BUDGET + tail at every turn, saw-tooth
   allowed; report the compaction-event count.
5. **Cost**: reported, not gated (decision input). Expectation to falsify: fresh/turn
   approaches the transcript arm's; total $ closes most of the 33% gap to Kimi at r=2%.
6. Liveness: composition oracle green on every turn + `external` fires on a planted
   out-of-band edit (a scripted mid-session file mutation is part of the s2 variant).

## 7. Failure modes -> what each teaches

- **Composition drift** (model mis-applies base+patches; mismatch rate rises): the safety net
  is the failed-edit回执 carrying the current region; if drift persists, v2 inserts a host
  auto-`external`+locator after N mismatches on one file. Weak-model ceiling is a finding, not
  a retry.
- **Tape churn** (rebase thrash on hot files, budget overflow on long sessions): tune
  REBASE_* / TAPE_BUDGET once, from the s7 run's measured distribution — thresholds are
  pre-registered as defaults, re-registered if changed, never silently tuned per scenario.
- **Compaction-event cost spikes**: if the saw-tooth's fresh spikes erase the steady-state win
  on s7, compaction granularity (per-topic instead of per-generation) is the named follow-up.

## 8. Honest positioning

This design intentionally moves the slice TOWARD the transcript: one append-only stream,
cache-priced history. What remains distinct — and is the restated moat — is that the stream is
**typed, host-verified, provenance-graded, redacted, deliberately compactable, and bounded**:
a curated tape with an eviction contract, versus an unbounded log with opportunistic
truncation. The cost story becomes "transcript-shaped billing with a ceiling", and the peak
story is the ceiling itself. Both claims are made only after the gates above, from ledgers.

## 9. Build ladder

- P-T0: this document survives adversarial review (spine-review pattern: refute-by-default).
- P-T1: tape store + per-type renderers + mechanical gates (offline).
- P-T2: host patch/base/external capture at the edit seam + composition oracle (offline).
- P-T3: layout integration at the single seam (both lanes) + lane parity.
- P-T4: byte + peak gates (API, n=2, s2+s7).
- P-T5: ability matrix (the P6 two-leg gate, tape vs spine-control).
