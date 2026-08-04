# Cost Architecture Review — fresh-in / fresh-out / cached-in

Date: 2026-08-05. Method: six-lane parallel investigation over the live repo + measured
ledgers (census + probe runs), every finding adversarially verified refute-by-default
(29 agents; **21 confirmed, 2 refuted**; full verdicts with file:line evidence in
`evals/census_runs/cost_review_verdicts.json`, local). Kimi source recon incorporated.
Moat constraints were hard rejection criteria (bounded peak, typed provenance+redaction,
sealed recoverability, frozen tape bytes, no task heuristics, quality gates).

## The three lanes, ranked by real dollars (DeepSeek v4 flash)

### FRESH-OUT ($0.28/M — the actual money; ~55% of every bill)
1. **Closeout verbosity** — no brevity contract existed anywhere in the prompt stack, and
   the NOW footer actively requested a "summary"; replies measured ~6.4k out-tok/10-turn
   episode, with edit enumerations duplicating the tape's [patch] entries byte-for-byte in
   prose. → SHIPPED (7008b4c): brevity contract in `<communication>` + NOW footer rewrite.
   Est. −$0.001–0.002/10-turn ep; scales with turn count (the s7 out-gap's substance).
2. **Parallel batching** — the loop is batch-safe end-to-end (verified: parsing, streaming,
   id pairing, dedup, T4, tape recorder, micro-compact), but the only prompt cue was a
   subordinate clause; measured 1.15–1.4 tools/call. Each avoided round-trip saves ~1.4k
   out (reasoning) + envelope replay. → SHIPPED: explicit MULTIPLE-calls-in-ONE-response
   rule. Est. up to −30% tool-bearing calls.
3. NEGATIVE (verified, don't spend here): str_replace old_string discipline already
   complied with (avg 156 chars); further anchoring is below measurement noise.

### FRESH-IN ($0.14/M)
4. **BUG (shipped unconditionally)**: the OPEN FILES index hashed RAW disk bytes while every
   tape entry hashes the REDACTED body — any file the redactor touches could NEVER satisfy
   composition rule 1, silently forcing rule-2 re-reads every turn. Index now hashes
   like-for-like. (Found while auditing a cost claim; worth more than the claim.)
5. **Tape entry shapes** → SHIPPED: bases un-numbered (7 chars/line = 14–17% of every base,
   and numbering fought the plain-diff patches); patches n=1 context, constant a/b labels
   (the path appeared 3×), wrapper shortened. ~−20% tape bytes.
6. Eager first-READ bases double-bill read-only files (README etc. enter the trajectory AND
   the tape). Candidate: defer base entry until first EDIT. PARKED pending a tape-liveness
   check (a read-only file's content would then live only in the sealed archive after its
   turn — recall-channel dependency). Revisit with s9 data.
7. NOW-frame constants (629 chars/boundary) hoistable to the system prefix — PARKED: prior
   layout A/Bs failed 4/4; salience-at-recency is a deliberate, measured choice.
8. OPEN FILES index collapse to a summary line — PARKED: removes the model-verifiable hash,
   the exact affordance class whose loss killed the recall channel (100%→38%).

### CACHED-IN ($0.0028/M — dollars ≈ 0 by construction)
9. Schema/system bytes are byte-stable call-to-call (verified: n_schemas constant, identical
   serialization every row) — the avoided counterfactual (~$0.07/session if churn broke the
   prefix) is already banked. No work here; batching (2) shrinks the echo volume as a side
   effect. **Do not spend engineering on this lane at r=2%.**

### COLD/PEAK appendix (not warm dollars, but peak IS a goal metric)
10. **Bench/production schema divergence**: the bench never binds the work-graph seam, so its
    calls carry 12.8k chars/call of with_note legacy schemas production doesn't send (45% of
    the schema payload). Fix = bench parity binding; also two tools that cannot succeed in
    the bench profile (change_workspace has no handoff consumer there). QUEUED (changes arm
    comparability — do between ladder stages, note in ledgers).
11. contextfs guidance was 62% a single-question answer script → SHIPPED (−2.3k chars/call).
12. Cross-tool routing prose consolidation: honestly tiny (−600 chars/call) — parked.

### Insurance (tail risk, $0 on measured runs — Kimi borrowings)
13. read_file has three unbounded lanes (offset-without-limit returns the file remainder;
    1500-line cap has no char bound; blob page-back uncapped); the kernel appends any tool
    result unconditionally (no last-resort cap — Kimi truncates at 50k/result with preview);
    micro-compact's recent-window blind spot can turn one oversized result into whole-turn
    destruction (~$0.018/event + a lost turn). QUEUED as batch 3: graduated per-result cap
    with loud marker + sealed locator (recoverable, moat-compatible), recent-window fix.

## Refuted (ammunition saved)
- "The meter cannot measure tools/call" — false; the census ledger reproduces it.
- "STABLE TASK OBJECTIVE is task-stable but for one corrections bit" — false;
  objective_status is per-turn recomputed and bidirectional.

## Kimi recon deltas that shaped this review
- Kimi replays reasoning_content for EVERY assistant message by default (keep:'all', even
  force-emitting empty fields on its native transport) — our provider-forced replay is not a
  competitive disadvantage; out-lane parity confirmed at the source level.
- Kimi compaction is a blocking cliff (85%/50k-reserve; one summarize-everything LLM call;
  only ≤20k of user prose survives; unrecoverable, /undo refuses the boundary). Our
  graduated GC + epoch fold + sealed archive is the contractual counter-design; s10 probes
  exactly this difference.
- Kimi's 50k/result truncation is worth stealing (item 13); its no-graduated-eviction
  cliff is worth avoiding (we already have micro-compact + tape GC).

## Measurement plan
Every shipped change reads out through the standing ladder (s2/s7 probe reruns + oracles +
liveness), not eyeballs: r5/r2 runs compare against r4/r1 at identical configs otherwise.
Prompt-behavior changes (1, 2) are watched for quality regression by the same oracles that
gate the ladder; a regression reverts the wording, not the goal.
