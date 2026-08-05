#!/usr/bin/env python3
"""Generate s10_compactloss — the forced-compaction information-loss probe (~30 turns).

Purpose: drive a transcript agent past its compaction cliff (kimi: used+50k >= window, ~78k
tokens on a 128k model; everything but <=20k of USER prose is destroyed, unrecoverable), then
quiz information that exists ONLY in assistant/tool history:
  - ONE-SHOT observation: inspect.py prints BUILD_ID derived from .build_seed, then DELETES the
    seed. Re-running later prints a LOUD tombstone. The user prompt forbids writing the id to
    any file. The only carriers are the agent's own memory structures.
  - EPHEMERAL error: linter output seen once at turn 7; the turn fixes the file, so re-running
    the linter later prints clean. The exact error string cannot be re-derived.
Sliceagent's carriers (typed findings/world state, tape reply/digest entries, sealed-turn
archive + search_history) survive seals by design; a transcript agent's only carrier is the
context window, which compaction destroys. Prompts are ARM-NEUTRAL: identical text, no
architecture named, and the memory instruction ("do not write it into any project file; keep
it in your working memory") is the same constraint for both arms.

BLOAT turns (read+one-line-summary of pre-seeded 9k-char data files) inflate context to force
the cliff BEFORE the quizzes. A run where the transcript arm never compacted is INVALID for
the loss claim — check wire.jsonl for context.apply_compaction (kimi) before interpreting.

Regenerate: .venv/bin/python benchmarks/multiturn_coding/_gen_compactloss.py
"""
from __future__ import annotations

import hashlib
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "s10_compactloss")

BUILD_ID = "bx-" + hashlib.sha256(b"s10-compactloss-build").hexdigest()[:10]
LINT_ERROR = "E9107 dangling-sentinel: 'qz_marker' bound but never sealed (frame 3)"
N_BLOAT = 64


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    rng = random.Random(9107)
    prompts: list[str] = []

    prompts.append("Read README.md and core.py to get oriented, then add a constant "
                   "VERSION = \"1.0\" to core.py.")
    prompts.append("Add a function checksum(text) to core.py returning the sum of byte values "
                   "modulo 65521.")
    prompts.append(
        "Run `python inspect_build.py` ONCE and note the build id it prints. You will need it "
        "much later. Do NOT write it into any project file or scratch note — keep it in your "
        "working memory. (The script consumes its seed: running it again will not reproduce "
        "the id.) Then add a constant BUILD_CHECKED = True to core.py.")
    prompts.append("Add a function clamp(v, lo, hi) to core.py.")
    prompts.append("Add a function window(seq, n) to core.py returning consecutive n-tuples.")
    prompts.append("Add a docstring to every function in core.py that lacks one (one line each).")
    prompts.append(
        "Run `python lint_frames.py` and fix the one issue it reports in frames.py (remove the "
        "offending line). Remember the EXACT error message text — you will be asked for it "
        "later; do not write it into any project file.")
    prompts.append("Add a function median(xs) to core.py (no imports).")
    for i in range(1, N_BLOAT + 1):
        prompts.append(
            f"Open data/blob_{i:02d}.txt and read the file IN FULL with your file-reading tool "
            f"— do not grep/search/extract just one line; the whole content must pass before "
            f"your eyes (it is an import audit). Then append ONE line to digest.md: "
            f"'blob_{i:02d}: <the 8-char token that appears after MARKER:>'.")
    prompts.append(
        "Quiz A: write the build id you noted earlier (just the id, one line) to "
        "answers/build_id.md. Do not run anything — the seed is gone; answer from memory.")
    prompts.append(
        "Quiz B: write the EXACT linter error message you saw and fixed earlier (one line, "
        "verbatim) to answers/lint_error.md. Do not re-run the linter — it reports clean now; "
        "answer from memory.")
    prompts.append(
        "Quiz C: in one line each, write to answers/timeline.md what the FIRST three things "
        "you did in this session were (the actual changes, in order).")
    prompts.append("Add a final constant SESSION_COMPLETE = True to core.py.")

    blobs = {}
    for i in range(1, N_BLOAT + 1):
        token = "".join(rng.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(8))
        filler = "\n".join(
            "".join(rng.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(96))
            for _ in range(90)
        )
        blobs[f"blob_{i:02d}.txt"] = (f"{filler[:4300]}\nMARKER: {token}\n{filler[4300:]}\n",
                                      token)

    setup = f'''import os

BUILD_ID = {BUILD_ID!r}
LINT_ERROR = {LINT_ERROR!r}
BLOBS = { {name: body for name, (body, _t) in blobs.items()} !r}


def setup(root):
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write("# compactloss substrate\\nSmall utility project. data/ holds import blobs; "
                "digest.md accumulates blob digests.\\n")
    with open(os.path.join(root, "core.py"), "w", encoding="utf-8") as f:
        f.write("# core utilities\\n")
    with open(os.path.join(root, "frames.py"), "w", encoding="utf-8") as f:
        f.write("FRAMES = [1, 2, 3]\\nqz_marker = object()   # sentinel bound for frame 3\\n")
    with open(os.path.join(root, ".build_seed"), "w", encoding="utf-8") as f:
        f.write(BUILD_ID + "\\n")
    with open(os.path.join(root, "inspect_build.py"), "w", encoding="utf-8") as f:
        f.write(
            "import os\\n"
            "if os.path.exists('.build_seed'):\\n"
            "    print('BUILD_ID: ' + open('.build_seed').read().strip())\\n"
            "    os.remove('.build_seed')\\n"
            "else:\\n"
            "    print('BUILD SEED CONSUMED: the id was shown exactly once and cannot be "
            "reproduced')\\n")
    with open(os.path.join(root, "lint_frames.py"), "w", encoding="utf-8") as f:
        f.write(
            "print(" + repr(LINT_ERROR) + " if 'qz_marker' in open('frames.py').read()"
            " else 'frames.py: clean')\\n")
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    for name, body in BLOBS.items():
        with open(os.path.join(root, "data", name), "w", encoding="utf-8") as f:
            f.write(body)
'''

    verify = f'''import os

BUILD_ID = {BUILD_ID!r}
LINT_ERROR = {LINT_ERROR!r}
TOKENS = { {f"blob_{i:02d}": t for i, (_n, (_b, t)) in
            enumerate(sorted(blobs.items()), 1)} !r}


def _read(root, rel):
    p = os.path.join(root, rel)
    return open(p, encoding="utf-8").read() if os.path.isfile(p) else ""


def verify(root):
    failed = []
    core = _read(root, "core.py")
    for needle in ("VERSION", "checksum", "clamp", "window", "median", "SESSION_COMPLETE"):
        if needle not in core:
            failed.append(f"core.py lacks {{needle}}")
    if "qz_marker" in _read(root, "frames.py"):
        failed.append("lint issue never fixed")
    digest = _read(root, "digest.md")
    missing = [b for b, t in TOKENS.items() if t not in digest]
    if missing:
        failed.append(f"digest tokens missing: {{missing[:4]}} (+{{len(missing)}} total)")
    # THE LOSS PROBES — graded separately so substrate failures don't mask them
    loss = []
    if BUILD_ID not in _read(root, "answers/build_id.md"):
        loss.append("QUIZ-A build id LOST")
    if LINT_ERROR.split(":")[0] not in _read(root, "answers/lint_error.md"):
        loss.append("QUIZ-B lint error LOST")
    tl = _read(root, "answers/timeline.md").lower()
    if not (("version" in tl) and ("checksum" in tl)):
        loss.append("QUIZ-C early timeline LOST")
    ok = not failed and not loss
    detail = "substrate+memory all hold" if ok else \\
        "LOSS: " + "; ".join(loss) + (" | substrate: " + "; ".join(failed[:6]) if failed else "")
    return ok, detail
'''
    json.dump(prompts, open(os.path.join(OUT, "prompts.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    json.dump({"name": 's10_compactloss', "turns": len(prompts), "stressor": 'forced transcript-compaction + assistant/tool-history-only information loss probes', "max_steps_per_turn": 12, "use_code_index": False},
              open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8"))
    open(os.path.join(OUT, "setup.py"), "w", encoding="utf-8").write(setup)
    open(os.path.join(OUT, "verify.py"), "w", encoding="utf-8").write(verify)
    print(f"wrote {OUT}: {len(prompts)} turns, {N_BLOAT} bloat blobs, "
          f"BUILD_ID={BUILD_ID}, 3 loss quizzes")


if __name__ == "__main__":
    main()
