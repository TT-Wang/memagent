#!/usr/bin/env python3
"""Offline tape-mechanics replay — the fast lane for compaction/representation policy work.

WHY: validating fold policy on a live 52-turn scenario costs ~45 min + $0.24 per data point,
and the thing being validated (fold sizing, byte accounting, prefix breakage) is DETERMINISTIC
MECHANICS — no model behavior involved. This harness replays an s11-shaped seal stream against
the real tape code in milliseconds:

  * real file bodies (snapshot of the s11 r2 workspace, evals/fixtures/s11_workspace/)
  * real 52 user prompts (benchmarks/multiturn_coding/s11_mixed_long/prompts.json)
  * measured edit mix (str_replace-dominated small deltas, appends, occasional rewrites,
    out-of-band drift at the measured s11 rate)

and computes the MECHANICAL fresh bill directly: per turn boundary, the longest common prefix
between consecutive rendered streams — bytes after the first divergence are what the provider
re-bills. Model-behavior costs (trajectory tool results, out tokens) are invariant across tape
policies and deliberately out of scope; the live scenario stays as the ONE graduation-time
behavior check, not the per-iteration loop.

Gates (s11-shaped, 52 turns):
  folds <= 3 · final tape chars <= budget · steady state (no fold thrash) ·
  boundary re-bill dominated by appends, not rewrites.

Run:  .venv/bin/python evals/tape_replay.py
"""
from __future__ import annotations

import json
import os
import random
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "packages", "sliceagent-core", "src"))

FIXTURE = os.path.join(_REPO, "evals", "fixtures", "s11_workspace")
PROMPTS = os.path.join(_REPO, "benchmarks", "multiturn_coding", "s11_mixed_long", "prompts.json")


class _FakeTools:
    def __init__(self, fs: dict):
        self.fs = fs

    def read_text(self, path: str) -> str:
        if path not in self.fs:
            raise FileNotFoundError(path)
        return self.fs[path]


class _Cont:
    def __init__(self):
        self.session_tape: list = []
        self.tape_files: dict = {}


class _S:
    def __init__(self):
        self.continuity = _Cont()


def _load_fixture() -> dict:
    fs = {}
    for root, _dirs, names in os.walk(FIXTURE):
        for n in names:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, FIXTURE)
            try:
                fs[rel] = open(p, encoding="utf-8").read()
            except UnicodeDecodeError:
                continue
    return fs


def _small_edit(body: str, rng: random.Random, tag: str) -> str:
    """A str_replace-shaped delta: touch 1-3 lines somewhere in the file."""
    lines = body.splitlines(keepends=True) or ["\n"]
    i = rng.randrange(len(lines))
    lines[i] = lines[i].rstrip("\n") + f"  # {tag}\n"
    if rng.random() < 0.3:
        lines.insert(min(i + 1, len(lines)), f"# added {tag}\n")
    return "".join(lines)


def replay(*, turns: int | None = None, budget: int | None = None,
           seed: int = 11, verbose: bool = False) -> dict:
    from sliceagent_core.spine import render_turn_digest
    from sliceagent_core.tape import TAPE_BUDGET_CHARS, tape_render, tape_seal_update

    budget = budget or TAPE_BUDGET_CHARS
    rng = random.Random(seed)
    fs = _load_fixture()
    prompts = json.load(open(PROMPTS, encoding="utf-8"))
    if turns:
        prompts = prompts[:turns]
    paths = sorted(p for p in fs if p.endswith(".py"))
    s = _S()
    tools = _FakeTools(fs)

    folds_total = 0
    drift_total = 0
    max_chars = 0
    boundary_bills: list[int] = []
    prev_stream = ""
    fold_turns: list[int] = []

    for t, ask in enumerate(prompts, 1):
        # measured s11 mix: ~4 edit events/turn, mostly small deltas; occasional rewrite;
        # a drift event (script/command mutation between seals) every ~6 turns
        rows = []
        for _ in range(rng.randrange(2, 6)):
            p = rng.choice(paths)
            if rng.random() < 0.06:
                fs[p] = f"# rewritten at turn {t}\n" + _small_edit(fs[p], rng, f"t{t}rw")
            else:
                fs[p] = _small_edit(fs[p], rng, f"t{t}")
            rows.append((p, fs[p]))
        if t % 6 == 0:
            p = rng.choice(paths)
            fs[p] = _small_edit(fs[p], rng, f"t{t}oob")     # out-of-band: no row recorded

        digest = render_turn_digest(artifact_id=f"t-{t:03d}", session_id="replay",
                                    task_id="s11", status="completed", user_request=ask)
        info = tape_seal_update(
            s, tools, rows, session_id="replay", artifact_id=f"t-{t:03d}", task_id="s11",
            status="completed", user_request=ask,
            assistant_reply=f"turn {t} done: " + "x" * rng.randrange(80, 700),
            digest_text=digest, budget=budget,
        )
        folds_total += info.get("epoch_folds", 0)
        if info.get("epoch_folds"):
            fold_turns.append(t)
        drift_total += info.get("drift", 0)

        stream = tape_render(s.continuity.session_tape)
        max_chars = max(max_chars, len(stream))
        if prev_stream:
            lcp = os.path.commonprefix([prev_stream, stream])
            boundary_bills.append(len(stream) - len(lcp))
        prev_stream = stream
        if verbose:
            print(f"t{t:02d} chars={len(stream):>7,} folds+={info.get('epoch_folds', 0)} "
                  f"drift+={info.get('drift', 0)}")

    final_chars = len(prev_stream)
    med_bill = sorted(boundary_bills)[len(boundary_bills) // 2] if boundary_bills else 0
    total_bill = sum(boundary_bills)
    out = {
        "turns": len(prompts), "folds": folds_total, "fold_turns": fold_turns,
        "drift": drift_total, "final_chars": final_chars, "max_chars": max_chars,
        "budget": budget, "boundary_bill_median": med_bill,
        "boundary_bill_total": total_bill,
    }
    return out


GATES = (
    ("folds <= 3", lambda r: r["folds"] <= 3),
    ("final_chars <= budget", lambda r: r["final_chars"] <= r["budget"]),
    ("no thrash (no adjacent fold turns)", lambda r: all(
        b - a > 1 for a, b in zip(r["fold_turns"], r["fold_turns"][1:]))),
    ("median boundary bill < 6k chars (append-shaped)",
     lambda r: r["boundary_bill_median"] < 6_000),
)


def main() -> int:
    r = replay(verbose="-v" in sys.argv)
    print(json.dumps(r, indent=1))
    ok = True
    for name, fn in GATES:
        good = fn(r)
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name}")
    print("MECHANICS", "GREEN" if ok else "RED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
