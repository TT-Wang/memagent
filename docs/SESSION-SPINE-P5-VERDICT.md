# Session Spine — P5 Byte Gate: Verdict & Diagnosis

> **ERRATUM (2026-08-05, post-verdict):** every WITHIN-turn "mutation" this document and its
> follow-ups reported was a measurement artifact. Comparing whole-list JSON serializations makes
> a perfect append look like a tail change: the previous request's closing `]` becomes `,`, so
> cp == len(a)−1 ALWAYS, and a survival *ratio* under append growth is arithmetic, not churn.
> Re-audited across all four captured runs (off r1, spine r1/r3, ctl r1): **zero real within-turn
> mutations — the engine was already perfectly append-only within a turn, before the projection
> pin.** Consequences: (1) the "NOW-footer migrates / per-call re-projection tax" analysis is
> WITHDRAWN; (2) the projection pin (9dd7357) is retained as a defensive guarantee (selection
> cannot drift under mid-turn capacity pressure) and a per-call compute saving, but its commit
> narrative overstated the motivating evidence; (3) same-turn "93–95% median" figures in this doc
> are the append-growth ratio, not a defect — the correct within-turn metric is append integrity
> (gap ≤ 1), now reported by the probe; (4) the external audit's warm-replay accounting (within-
> turn replay is cache-priced) stands as written. CROSS-turn findings are unaffected: turn-boundary
> pairs are real rebuilds and every cross-turn number and conclusion in this document stands.
> New trap for the catalogue: serialized-list prefix comparison — measure strict-prefix extension,
> never ratio, for append-shaped sequences.

Date: 2026-08-05. Status: **GATE FAILED — mechanism PROVEN, ratio ceiling is structural.**
Stopped by owner decision after 3 instrumented runs (off r1, spine r1 pre-layout-fix, spine r3
post-fix); p3 baseline runs cancelled — their result is derivable (see §4). Evidence:
`evals/spine_probe_runs/` (+ `archive-prelayout/`). Probe: `evals/spine_probe.py` (v2
attribution). Pre-registered criteria: `evals/spine_probe_verdict.py` (committed before data).

## 1. The three runs (s2_taskdag_scheduler, DeepSeek v4 flash, real memory)

| run | layout | cross-turn median | same-turn median | break location | fresh tok | cost |
|---|---|---|---|---|---|---|
| off r1 | legacy | 39.9% (n=9) | 92.1% | `ACTIVE USER INTENT` ×6 | 93k | $0.0335 |
| spine r1 | spine at slot 2 (below intent) | 33.6% | 95.0% | `ACTIVE USER INTENT` ×6 — **same byte offsets as off** | — | — |
| spine r3 | **fixed: [head][SPINE][tail]** (7980f46) | 33.6% | 93.1% | `SESSION SPINE` ×8 — **cp grows 16.9k→21.2k** | 140k | $0.0454 |

All runs passed their behavioral oracle; liveness green (episodes archived, spine rendered).

## 2. What is PROVEN

- **The append-only mechanism works exactly as designed.** After the layout fix the surviving
  prefix grows monotonically (+~400–600 chars/turn = the frozen digests), and the first changed
  byte is the spine's newest entry — the "ramps to head+B" shape from the design. Byte-level,
  attributable, reproducible.
- **The layout gap was real and the gate caught it** (spine r1: identical break offsets to
  control at 17,137/18,309/17,390… — the frozen record sat entirely below the first changed
  byte). Root cause fixed and unit-gated (`test_spine_layout_head_precedes_volatile_regions`).

## 3. Why the RATIO cannot reach 80% on this workload

LCP accounting is positional: identical bytes AFTER the first divergence earn nothing. Under
the fixed layout the survivable prefix is `system+envelope (~17k chars) + old spine (~0.5k/turn)`,
while the request grows 29.8k→74k chars because the midsection — OPEN FILES re-projected each
turn, accumulated findings, related-code, worktree — is ~70% of the seed and sits (necessarily)
below the spine's growth point. Net saving vs control ≈ +3.5k chars/turn-boundary by turn 10
(~6% of the rebuild) while the spine itself adds ~0.5k chars to EVERY request. The meters agree:
spine r3 billed MORE than control this run (+35%, call-count variance included).

**The structural statement:** prefix caching rewards append-only request shapes; the slice's
moat (bounded peak) comes precisely from NOT being append-only — re-projecting the workspace
fresh each turn. The two goals trade off through the size of the per-turn tail. The spine fixes
the CONTINUITY portion of the rebuild (conversation window, manifest, turn history), but on
file-heavy work that portion was never the bulk — the workspace projection is.

## 4. The cancelled p3 arm (derivable, recorded for honesty)

p3-only = git line out of system prefix + task-keyed knowledge memo. Both target the SYSTEM
message — which already survived byte-identically in every off/spine pair (breaks are all in
msg1). Within a single session the system git snapshot is computed once anyway, so p3 ≈ off on
this probe (cross-turn ≈ 39.9%). The spine-vs-p3 delta criterion (+10pp) would NOT have been
met: the growing-spine credit is +2–6pp at s2 lengths. Recorded as a derivation, not a
measurement; re-run if this figure ever becomes load-bearing.

## 5. Verdict against the pre-registered criteria

1. spine cross-turn ≥80% absolute — **FAIL** (33.6%)
2. ≥+10pp over p3-only — **FAIL** (derived +0–6pp against p3≈off)
3. same-turn ≥96% — FAIL narrowly (93.1%; control 92.1% — no regression, target optimistic)
4. zero liveness invalids — PASS

Per the roadmap: failure = stop and diagnose; no quality runs on an unproven mechanism. The
mechanism is proven; the TARGET was mis-calibrated for file-heavy scenarios — 80% requires the
per-turn tail ≤20% of the request, and s2's tail is ~70%.

## 6. What the spine is still worth (unchanged by this verdict)

- Provenance-grade frozen history with R10 epistemic honesty + R4 recall locators — the
  correctness/continuity value never depended on cache economics.
- Same-turn stability tied best-ever measurement (95.0% in r1's config) via manifest
  suppression + reserve boundary.
- On light-tail workloads (conversational s6, long-horizon s7 where the seed is small and the
  spine becomes the bulk), the survival ratio should rise with session length — UNTESTED;
  requires its own probe before claiming.

## 7. Options (decision owner: TT-Wang)

- **A. Re-scope (cheap):** ship the spine for continuity/provenance + light-tail workloads;
  re-register the byte gate per workload class (file-heavy: judge by absolute chars saved, not
  ratio). Cost story on file-heavy stays with T4 + output diet.
- **B. Midsection subsumption (the thesis endgame):** make the request effectively append-only
  within a task — OPEN FILES leaves the per-turn seed (locators + hashes + read-on-demand;
  the freeze experiment already proved a turn-start snapshot suffices within-turn), findings
  become an append-only ledger. Transcript-shaped BILLING with slice-shaped PEAK. High quality
  risk (stale-view hazards on constraint-discipline scenarios); needs its own design review +
  pre-registered A/B program.
- **C. Park the spine:** keep P3 head stability + T4; flag stays default-off. Zero risk, zero
  win beyond what's shipped.

Recommendation: A now (the spine is honest continuity infrastructure at negligible cost when
the flag is off), with B written up as the next design review — it is the "cache-not-log"
thesis taken to its logical end, and the byte probe built here is exactly the instrument that
program needs.
