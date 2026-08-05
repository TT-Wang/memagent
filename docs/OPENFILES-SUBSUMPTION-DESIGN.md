> **RETIRED (tape graduation, 2026-08-05)** — the standalone locators flag described here was deleted in wave 1 (docs/TAPE-GRADUATION.md). The document remains as the evidence chain; the runnable historical arm lives at git tag `lab-2026-08-05`.

# OPEN FILES Subsumption — Option B Pre-Registered Experiment Design (DRAFT)

Status: DRAFT for owner review (TT-Wang). No code implemented. Date: 2026-08-05.
Antecedents: `SESSION-SPINE-P5-VERDICT.md` (§7 Option B) · `SESSION-SPINE-ROADMAP.md`
(measurement discipline) · external audit `task144-schema-shape-audit.md` (items F, "What not
to do") · freeze experiment (`AGENT_FREEZE_OPEN_FILES`, seed.py).

## 1. Motivation

The P5 byte gate proved the spine mechanism and simultaneously proved its ceiling: cross-turn
prefix survival on file-heavy s2 is structurally capped at 33.6% (control 39.9%), because the
OPEN FILES/findings midsection is ~70% of the request and re-bills below the spine every turn —
the surviving prefix grows only 16.9k→21.2k chars while the request grows 29.8k→74k. The
verdict's structural statement, quoted in full because this design is its direct answer:
"prefix caching rewards append-only request shapes; the slice's moat (bounded peak) comes
precisely from NOT being append-only — re-projecting the workspace fresh each turn. The two
goals trade off through the size of the per-turn tail." Option B shrinks the per-turn tail to
one screen of locators: file BODIES leave the seed and move to read-on-demand tool results in
the within-turn trajectory (append-only, cache-friendly); untouched files leave the bill
entirely. Target billing shape: `[head][spine][one screen of locators][ask]` — near-append-only,
with slice-shaped peak. The audit's census confirms the target is the dominant payload
(real-repo profile: ~1,000,256 estimated OPEN FILES tokens vs 27,307 RELATED CODE).

## 2. The central bet, stated honestly

This design's entire risk is a dead-affordance failure, and this project has measured that
failure twice: the recall channel was alive and correct with recalls=0 (models do not reach for
available pull-channels), and the cache manifest's usage went 38%→100% ONLY after it was made
visible with a bounded locator + copy-paste call syntax. Bare paths do not work. The winning
recipe — visible locator + exact call syntax + freshness signal — is baked into every line of
§3, and the liveness metric (§5) treats "model never reads" as an INVALID run, not a cost win.

The ability bar is provably reachable: Kimi/mini pass the same 8-scenario matrix with NO live
file view at all, working purely from read results in the transcript. The difference is their
context grows without bound — which is exactly the slice's moat. Option B asks the slice to
work the way a transcript agent works within a turn, while keeping the sealed-boundary peak.

Per the audit: "Do not optimize OPEN FILES away without an edit/verification ability gate."
The ability gate in §5 is that gate, and it is the whole bet.

## 3. Design: two coupled halves (both ship together or neither)

Half A (pull discipline) without Half B (visible locators) reproduces the dead recall channel;
Half B without Half A reproduces the 38% manifest. They are one change behind one flag.

### 3a. Half A — system-prompt standing discipline (DRAFT text, verbatim)

Inserted into `SYSTEM_PROMPT` (prompt.py) when the flag is on, replacing the current
"OPEN FILES ... establish current world state / base edits on OPEN FILES" framing:

```
WORKSPACE FILES appear as LOCATORS, not contents. Each OPEN FILES line shows a file's
path, line count, content hash, and the exact call to view it. A file's contents are NOT
in your context until you read_file() it.

Non-negotiable discipline:
1. Before ANY edit (str_replace / write / insert), call read_file("path") for that file in
   THIS turn — unless you already read it this turn, or your own successful edit this turn
   already showed you the resulting content.
2. The sha256 in a locator is the file's on-disk fingerprint AT THE START OF THIS TURN. If
   it differs from the hash you saw last turn, the file changed between turns — your memory
   of it is STALE; re-read before acting.
3. "(edited this session)" marks files you changed in earlier turns. On-disk truth is the
   locator's hash, never your memory of the edit.
4. Never reconstruct file contents from the path, your notes, or earlier turns. A read is
   one cheap call; an edit aimed at remembered text wastes a whole step.
```

### 3b. Half B — the locator region

`build_artifacts` body rendering is bypassed; the open_files region renders ONE line per file:

```
### <path> — <N> lines · sha256:<hash12> · read_file("<path>") to view
### <path> — <N> lines · sha256:<hash12> · read_file("<path>") to view · (edited this session)
```

Exact fields:
- `<path>` — resolved through the same seam as today (`resolve_read`), so display agrees with
  where edits land.
- `<N> lines` — line count at snapshot time (bounded size cue: is this a 40-line or 4,000-line
  read).
- `sha256:<hash12>` — first 12 hex chars of sha256 over file bytes at snapshot. This is the
  freshness signal (recipe leg 3) and the stale-read tripwire.
- `read_file("<path>") to view` — literal copy-paste call syntax (recipe leg 2; the exact
  mechanism that took manifest usage 38%→100%).
- `· (edited this session)` — appended for `p in s.edited_files`.

Order preserved from today (edited files sorted first, then reads sorted — byte-stable across
steps). Existing error states carry over as one-line locators without a hash: `(not created
yet)`, `(exists on disk; outside file-tool reach — inspect via run_command)`, `(exists but not
shown: <reason>)`.

Region header replaces the current "live — your ground truth; edit based on this" framing
(regions.py open_files RegionSpec), which would be a lie under locators:

```
# OPEN FILES (locators — contents NOT in context; disk is ground truth; read_file before
editing; a changed hash means your last read is stale)
```

### 3c. Knock-on decisions

- **read_budget**: render-time view tightening becomes a no-op under the flag — a locator line
  is ~100 chars, so the FULL resident working set (edited + protected + all resident reads) is
  listed. SwapManager eviction still bounds the durable set; the bound stays relevance/growth,
  now at negligible render cost. `s.read_budget` and its refault-growth machinery are untouched
  (control path unchanged).
- **FULL_FILE_LINES / REGION_LINES / `_relevant_regions`**: unused on the flagged seed path;
  untouched for control and for any other caller. Not deleted in this change.
- **AGENT_FREEZE_OPEN_FILES**: subsumed. The locator block is snapshotted at turn START (same
  key: slice identity + turn ordinal), inheriting the freeze experiment's proven semantics — a
  turn-start snapshot suffices within a turn because the model's own edits/reads are visible in
  the trajectory. Setting both flags is harmless; locators win.
- **Pressure fallback**: the existing paged fallback for open_files ("paged under context
  pressure — re-read live before acting", bare `read_file` bullets) converges with the primary
  render; under the flag the degraded alternative is the same shape minus hashes.
- **Flag**: `AGENT_OPENFILES_LOCATORS=1`. Default off. Composable with `AGENT_SESSION_SPINE=1`;
  the composed configuration is the actual target of the byte gate (§5).

## 4. What does NOT change (explicit)

- **findings region** — unchanged this experiment. (The verdict's "findings become an
  append-only ledger" is a SEPARATE follow-on; bundling it here would confound attribution.)
- **worktree region** — live git state stays live.
- **paired verbatim reserve** — untouched (roadmap invariant 1; audit "What not to do").
- **spine** — mechanism and layout exactly as shipped at P5; this design changes only what
  sits BELOW it.
- **RELATED CODE / memory / skills / trajectory & micro-compact / SwapManager eviction /
  tool schemas** — all unchanged.
- **read_file tool semantics** — unchanged; no new tool.

## 5. Pre-registered experiment

Per the roadmap's standing rules: n=2 minimum before any verdict; byte gates before behavior
gates; these criteria are committed BEFORE any run; a failed config gets failure-mode analysis
(§6), not retries-until-green; every row carries liveness fields; the 13-trap catalogue applies.

**Arms** (both with `AGENT_SESSION_SPINE=1`, DeepSeek v4 flash, real memory — matching P5):
- control: current spine layout (full-body OPEN FILES) — the 33.6% configuration.
- treatment: `AGENT_OPENFILES_LOCATORS=1` + discipline prompt.

**Scenarios, n=2 each (8 runs total):**
- `s2_taskdag_scheduler` — file-heavy; the exact workload where the 33.6% ceiling was measured
  and the tail is ~70%. If Option B works anywhere it must work here; reusing s2 makes the
  before/after directly comparable at identical byte offsets.
- s7 long-horizon (50-turn accumulation, roadmap P6) — stresses the two things s2 cannot:
  discipline retention late in long sessions (named risk a) and read-round-trip overhead
  compounding across many turns (named risk b). Also the moat showcase (flat peak curve).

**Metrics and PASS thresholds:**

| Metric | Instrument | PASS threshold |
|---|---|---|
| Ability gate (audit item F) | scenario verify.py oracles; run logs | ALL behavioral oracles pass on treatment, AND zero max_steps abnormal stops, AND str_replace old_string mismatch rate ≤ control's |
| Byte gate | `evals/spine_probe.py` (v2 attribution) | treatment cross-turn prefix survival ≥80% median — the number the spine alone could not reach |
| Cost | run ledgers (fresh tokens, $) | REPORTED, no threshold — decision input, not a gate |
| Liveness | read_file calls per turn | >0 on every turn that edits; a treatment run where the model never reads is INVALID (dead affordance), not a win |

Same-turn survival is reported for regression watch (control r3 baseline 93.1%; the projection
pin of 9dd7357 should already have moved this — re-baseline control first) but is not a gate
here. Verdict script committed before data, as in P5 (`spine_probe_verdict.py` pattern).
The ability gate is evaluated FIRST; a treatment that aces the byte gate but fails ability is
a failed config, full stop — cheap tokens from an agent that cannot edit are worth nothing.

**Pre-registered attribution rule (no post-hoc goalpost moves):** if the treatment fails the
80% byte gate but the probe's break attribution shows the residual breaks are in regions this
experiment deliberately does NOT touch (findings / conversation reserve / intent family), that
is recorded as MECHANISM PASS · GATE BLOCKED-BY-NEIGHBOR: the locator change did its own job,
and the named neighbor (in practice the findings ledger follow-on) becomes the next
pre-registered arm. Only breaks attributed to the OPEN FILES locator block itself, or an
ability-gate failure, count against THIS design.

## 6. Failure modes and what each would teach

- **(a) Discipline decay** — weak models forget read-before-edit late in long turns.
  Signature: edit-failure rate climbs with turn index; max_steps spins on s7. Teaches: prompt
  discipline alone is insufficient at this model strength — the follow-up is a HOST-side gate
  (reject an edit to a file with no fresh-read receipt this turn, returning the locator line),
  i.e. move the invariant from persuasion to enforcement. Not a retry; a redesign.
- **(b) Read round-trip tax** — steps/turn and wall-clock rise enough to erase the token win.
  Signature: oracles pass but steps/turn ≫ control and $/scenario ≥ control. Teaches: the pure
  form trades too much latency; the fallback is the audit's item F hybrid — full text for the
  ACTIVE EDIT TARGET only, locators for everything else — a new pre-registered arm, not a tweak
  to this one.
- **(c) Stale-view corruption** — the model edits from remembered content despite the hash.
  Signature: str_replace mismatch rate > control; worst case a WRONG edit that happens to
  apply. Teaches: the freshness signal is displayed but not consulted — same lesson as (a),
  host-side stale-read rejection (hash at read ≠ hash at edit → refuse with the new locator).
  Any silent-wrong-edit instance is a hard stop for the whole direction pending that gate.

Each failure is diagnosed against per-turn logs before any new run is scheduled.

## 7. Rollout

- Flag default-off. Ships inert; control path byte-identical with the flag unset (unit-gated,
  same pattern as the spine layout test).
- Graduation to default-considered: BOTH gates pass at n=2 on BOTH scenarios, THEN the full
  8-scenario matrix ability gate (P6 execution leg, treatment vs control, n=2, per-scenario
  oracle parity) — s5/s6 conversational canaries included, since constraint-discipline
  scenarios are where the verdict flagged stale-view hazard.
- Only after matrix parity: rerun the P9 endgame numbers (cost vs Kimi at r=2%, peak curve)
  and restate public claims from post-locator ledgers — never renumber without restating.
- Kill criterion: any confirmed silent-wrong-edit (mode c) without a host-side gate in place
  keeps the flag off regardless of byte-gate results.
