#!/usr/bin/env python3
"""Generate s12_regression — the PASS_TO_PASS no-regression probe (quality roadmap P2).

Purpose: today's scenarios verify "did the asked feature land"; NONE verify "did nothing else
break". This scenario plants DISTANT CALLERS whose behavior depends on properties of helpers
the prompts repeatedly ask the agent to edit — the classic fixed-but-broke-something shape:

  * mathlib/stats.py depends on seq.normalize returning a NEW ascending list (not in-place,
    not a generator) and on scale() keeping list length;
  * mathlib/report.py depends on stats.summary's key names and on normalize dropping
    duplicates STABLY (first occurrence wins for the label map).

The prompts NEVER mention stats.py/report.py behaviors — a careful agent greps callers before
changing a helper's contract; a careless one "improves" normalize and silently breaks them.
Verification separates the two failure classes exactly like s10 separates LOSS:

  detail = "REGRESS: <frozen behaviors broken> | <asked features missing>"

Headline metric = the REGRESS section (fixed-but-broke-something), the #1 moat-at-risk probe.
Regenerate: .venv/bin/python benchmarks/multiturn_coding/_gen_regression.py
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "s12_regression")

P: list[str] = []
CHECK: list[tuple[str, str]] = []      # (python -c probe, label) — asked features
REGRESS: list[tuple[str, str]] = []    # (python -c probe, label) — frozen distant behaviors


def turn(prompt: str, probe: str | None = None, label: str = "") -> None:
    P.append(prompt)
    if probe:
        CHECK.append((probe, label or f"t{len(P)}"))


def main() -> None:
    os.makedirs(OUT, exist_ok=True)

    # ---- turns: every edit target is a helper with unseen distant callers
    turn("Read README.md and the mathlib/ package to orient. Then add a module docstring to "
         "mathlib/seq.py describing each helper in one line.",
         "import mathlib.seq as q; assert q.__doc__ and 'normalize' in q.__doc__", "docstring")
    turn("Add median(seq) to mathlib/stats.py (no imports; even-length averages the middle "
         "pair). Quick check with a couple of calls.",
         "import mathlib.stats as st; assert st.median([3, 1, 2]) == 2 and "
         "st.median([4, 1, 3, 2]) == 2.5", "median")
    turn("normalize() in mathlib/seq.py should also accept tuples and generators, not just "
         "lists. Keep its behavior otherwise.",
         "import mathlib.seq as q; assert q.normalize((3, 1, 2)) == [1, 2, 3] and "
         "q.normalize(x for x in [2, 1]) == [1, 2]", "normalize inputs")
    turn("Add clamp(seq, lo, hi) to mathlib/seq.py returning a new list with values clamped.",
         "import mathlib.seq as q; assert q.clamp([0, 5, 10], 1, 8) == [1, 5, 8]", "clamp")
    turn("scale(seq, k) currently multiplies; make k=0 return a list of zeros of the SAME "
         "length (it already does — add an explicit test for it plus a docstring note).",
         "import mathlib.seq as q; assert q.scale([1, 2], 0) == [0, 0]", "scale k=0")
    turn("normalize() should drop NaN values (float('nan')) before sorting. Tests.",
         "import mathlib.seq as q; r = q.normalize([2.0, float('nan'), 1.0]); "
         "assert r == [1.0, 2.0]", "normalize NaN")
    turn("Quick one: add VERSION = '0.2' to mathlib/__init__.py.",
         "import mathlib; assert mathlib.VERSION", "version")
    turn("Add a percentile(seq, p) to mathlib/stats.py (nearest-rank method). Tests.",
         "import mathlib.stats as st; assert st.percentile([1, 2, 3, 4], 50) in (2, 2.5, 3)",
         "percentile")
    turn("normalize() is called a lot — micro-optimize it (avoid double iteration if easy) "
         "while keeping the exact same output contract. Show a quick before/after check.",
         "import mathlib.seq as q; assert q.normalize([2, 1, 2]) == [1, 2]", "normalize perf")
    turn("Add top_k(seq, k) to mathlib/seq.py returning the k largest values, descending.",
         "import mathlib.seq as q; assert q.top_k([1, 3, 2], 2) == [3, 2]", "top_k")
    turn("Write mathlib/cli.py: `python -m mathlib.cli summary 1 2 3` prints the summary dict.",
         "import subprocess, sys; out = subprocess.run([sys.executable, '-m', 'mathlib.cli', "
         "'summary', '1', '2', '3'], capture_output=True, text=True).stdout; "
         "assert 'mean' in out", "cli")
    turn("Final: add CHANGELOG.md with one line per change made this session.",
         "import os; assert os.path.isfile('CHANGELOG.md')", "changelog")

    # ---- FROZEN distant behaviors (never mentioned in any prompt)
    REGRESS.extend([
        ("import mathlib.seq as q; a = [3, 1, 2]; r = q.normalize(a); "
         "assert r == [1, 2, 3] and a == [3, 1, 2] and r is not a",
         "normalize returns a NEW list, input untouched (stats.py depends on it)"),
        ("import mathlib.seq as q; r = q.normalize([2, 1, 2, 1]); assert r == [1, 2]",
         "normalize dedupes ascending (report.py label map depends on it)"),
        ("import mathlib.stats as st; s = st.summary([1, 2, 3]); "
         "assert set(s) >= {'mean', 'lo', 'hi'} and s['mean'] == 2 and s['lo'] == 1",
         "stats.summary key names + values (report.py reads these keys)"),
        ("import mathlib.seq as q; assert len(q.scale([1, 2, 3], 2)) == 3 and "
         "q.scale([1, 2, 3], 2) == [2, 4, 6]", "scale preserves length and order"),
        ("import mathlib.report as rp; text = rp.render([3, 1, 2]); "
         "assert 'mean=2' in text and 'range=1..3' in text",
         "report.render end-to-end format (the distant caller chain)"),
    ])

    setup = '''import os


def setup(root):
    os.makedirs(os.path.join(root, "mathlib"), exist_ok=True)
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write("# mathlib\\nSmall numeric helpers. mathlib/seq.py holds sequence helpers used "
                "across the package; stats/report build on them.\\n")
    with open(os.path.join(root, "mathlib", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")
    with open(os.path.join(root, "mathlib", "seq.py"), "w", encoding="utf-8") as f:
        f.write(
            "def normalize(seq):\\n"
            "    out = []\\n"
            "    for v in sorted(seq):\\n"
            "        if not out or out[-1] != v:\\n"
            "            out.append(v)\\n"
            "    return out\\n"
            "\\n"
            "\\n"
            "def window(seq, n):\\n"
            "    items = list(seq)\\n"
            "    return [tuple(items[i:i + n]) for i in range(len(items) - n + 1)]\\n"
            "\\n"
            "\\n"
            "def scale(seq, k):\\n"
            "    return [v * k for v in seq]\\n")
    with open(os.path.join(root, "mathlib", "stats.py"), "w", encoding="utf-8") as f:
        f.write(
            "from .seq import normalize\\n"
            "\\n"
            "\\n"
            "def summary(seq):\\n"
            "    vals = normalize(seq)\\n"
            "    # relies on normalize returning a NEW ascending list\\n"
            "    return {\\"mean\\": sum(seq) / len(list(seq)), \\"lo\\": vals[0], \\"hi\\": vals[-1]}\\n")
    with open(os.path.join(root, "mathlib", "report.py"), "w", encoding="utf-8") as f:
        f.write(
            "from .stats import summary\\n"
            "\\n"
            "\\n"
            "def render(seq):\\n"
            "    s = summary(seq)\\n"
            "    mean = int(s[\\"mean\\"]) if float(s[\\"mean\\"]).is_integer() else s[\\"mean\\"]\\n"
            "    return f\\"mean={mean} range={s['lo']}..{s['hi']}\\"\\n")
'''

    verify = f'''import subprocess
import sys

CHECKS = {CHECK!r}
REGRESS = {REGRESS!r}


def _run(root, probe):
    r = subprocess.run([sys.executable, "-c", probe], cwd=root,
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def verify(root):
    missing = [label for probe, label in CHECKS if not _run(root, probe)]
    broken = [label for probe, label in REGRESS if not _run(root, probe)]
    ok = not missing and not broken
    detail = ("all {{}} features + {{}} frozen behaviors hold".format(len(CHECKS), len(REGRESS))
              if ok else
              ("REGRESS: " + "; ".join(broken) + " | " if broken else "")
              + ("missing: " + "; ".join(missing) if missing else ""))
    return ok, detail
'''

    json.dump(P, open(os.path.join(OUT, "prompts.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    json.dump({"name": "s12_regression", "turns": len(P),
               "stressor": "PASS_TO_PASS: planted distant callers depend on edited helpers' "
                           "contracts; headline = fixed-but-broke-something (REGRESS section)",
               "max_steps_per_turn": 12, "use_code_index": False},
              open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8"))
    open(os.path.join(OUT, "setup.py"), "w", encoding="utf-8").write(setup)
    open(os.path.join(OUT, "verify.py"), "w", encoding="utf-8").write(verify)
    print(f"wrote {OUT}: {len(P)} turns, {len(CHECK)} feature checks, "
          f"{len(REGRESS)} frozen distant behaviors")


if __name__ == "__main__":
    main()
