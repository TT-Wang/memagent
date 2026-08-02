"""System-prompt VARIANTS for the A/B suite.

Each variant is a SINGLE-VARIABLE transform of the live production SYSTEM_PROMPT (the control),
materialized to a full prompt file (keeping the {{MEMORY_MODEL}} marker) that the driver injects via the
SLICEAGENT_PROMPT_FILE seam in prompt.py. Every transform ASSERTS its anchor strings exist, so if the prompt
is edited and an anchor moves, the build fails LOUDLY instead of silently emitting a wrong variant.

Catalog (ranked, cited) is in the prompt-ab-catalog memory. Implemented here = the 5 highest-confidence
bets (#1 #2 #3 #4 #7) + the dedupe NEGATIVE CONTROL. Add more by writing another transform + registry row.

  PYTHONPATH=src .venv/bin/python -m evals.prompt_ab.variants    # build all variant files
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
VARIANTS_DIR = os.path.join(HERE, "variants")


def control_text() -> str:
    """The RAW production SYSTEM_PROMPT template (seam OFF), captured via a clean subprocess so it is
    byte-exactly what ships. Contains the {{MEMORY_MODEL}} marker."""
    env = {k: v for k, v in os.environ.items() if k != "SLICEAGENT_PROMPT_FILE"}
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    code = "import sys; from sliceagent.prompt import SYSTEM_PROMPT; sys.stdout.write(SYSTEM_PROMPT)"
    p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    return p.stdout


def _require(text: str, anchor: str) -> None:
    if anchor not in text:
        raise AssertionError(f"prompt-ab anchor not found (did the prompt change?): {anchor[:70]!r}")


# ---- anchors (verbatim substrings of prompt.py SYSTEM_PROMPT) ----
ASK_OPEN = "<ask>\n"
TIERS_START = "The slice is organized into TIERS. Trust them in this order of AUTHORITY (highest first):\n"
WORK_OPEN = "\n<work>\n"
VERIF_OPEN = "<verification>\n"
VERIF_CLOSE = "</verification>\n\n"
SAFETY_END = "</safety>"
TIER2_DUP = " — your own note saying 'done' does NOT clear a user report"
TIER4_DUP = ", and a note that says the work is 'done' is NOT proof — confirm it on the real artifact first"

# verbatim load-bearing clauses re-used by the recency-anchor variants
VERIFY_CLAUSE = ("Before you state ANYTHING as true — a bug, a root cause, 'this is correct', 'this is done' "
                 "— CONFIRM it against the code or a tool result (avoid hallucination, fact-check first): "
                 "report the issues you have actually traced and confirmed, and do not report a "
                 "plausible-looking concern you have not confirmed.")
DONE_CLAUSE = ("'Done' means the task's REAL end-state holds in the world; confirm it DIRECTLY "
               "(run / open / observe it) — your own note saying 'done' is never proof.")
ACTCONV_CLAUSE = ("If the message is a greeting, a question, or a request to explain, plan, or discuss, your "
                  "turn ends with a TEXT reply and NO tool call.")


def v_recency_verify(t: str) -> str:
    """#1 — append a closing <reminder> that VERBATIM-copies the two verify/confirm clauses (deletes nothing).
    Primacy+recency: the prompt currently ends on <safety>, parking the highest-stakes constraint out of the
    recency slot. Our own A/B proved repeating these clauses cuts review false-positives."""
    _require(t, SAFETY_END)
    return t + f"\n\n<reminder>\n{VERIFY_CLAUSE} {DONE_CLAUSE}\n</reminder>"


def v_recency_actconverse(t: str) -> str:
    """#3 — append a closing <reminder> that VERBATIM-restates the act-vs-converse rule (recency anchor for
    the decision made last, right before responding). Separate arm from #1 to stay single-variable."""
    _require(t, SAFETY_END)
    return t + f"\n\n<reminder>\n{ACTCONV_CLAUSE}\n</reminder>"


def v_lead_verification(t: str) -> str:
    """#2 — MOVE the whole <verification> block (verbatim, no word change) to before <ask>, out of the
    lost-in-the-middle dead zone. Reorder only."""
    _require(t, VERIF_OPEN)
    _require(t, VERIF_CLOSE)
    _require(t, ASK_OPEN)
    s = t.index(VERIF_OPEN)
    e = t.index(VERIF_CLOSE) + len(VERIF_CLOSE)
    block = t[s:e]
    t2 = t[:s] + t[e:]
    i = t2.index(ASK_OPEN)
    return t2[:i] + block + t2[i:]


def v_precedence_tag(t: str) -> str:
    """#4 — wrap the TIERS authority list in <precedence>...</precedence> (tag only; no wording/order change).
    Gives the moat's world-over-your-note constitution an attendable, named home."""
    _require(t, TIERS_START)
    _require(t, WORK_OPEN)
    s = t.index(TIERS_START)
    e = t.index(WORK_OPEN)
    return t[:s] + "<precedence>\n" + t[s:e] + "</precedence>\n" + t[e:]


def v_dedupe_control(t: str) -> str:
    """NEGATIVE CONTROL — do NOT ship. Re-creates the measured-regressive dedupe (commit 773033a): removes
    the REPEATED verify/confirm reminders from tiers 2 & 4 (canonical copy stays in <verification>). Its job
    each batch is to re-confirm the FP-doubling baseline; if it does NOT regress, the harness/judge drifted."""
    _require(t, TIER2_DUP)
    _require(t, TIER4_DUP)
    return t.replace(TIER2_DUP, "").replace(TIER4_DUP, "")


# name -> transform (None = the raw production prompt). Order = report order; control first.
VARIANTS = {
    "control": None,
    "v01_recency_verify": v_recency_verify,
    "v02_lead_verification": v_lead_verification,
    "v03_recency_actconverse": v_recency_actconverse,
    "v04_precedence_tag": v_precedence_tag,
    "ctrl_dedupe": v_dedupe_control,
    # v07_concise_floor — SHIPPED to slice.py 2026-06-30 (clear measured length win); now part of control.
}


def variant_path(name: str) -> str:
    return os.path.join(VARIANTS_DIR, name + ".txt")


def list_variants() -> list[str]:
    return list(VARIANTS.keys())


def build_all() -> list[str]:
    """Materialize every variant to evals/prompt_ab/variants/<name>.txt. Idempotent."""
    os.makedirs(VARIANTS_DIR, exist_ok=True)
    base = control_text()
    if "{{MEMORY_MODEL}}" not in base:
        raise AssertionError("control prompt missing {{MEMORY_MODEL}} marker")
    written = []
    for name, fn in VARIANTS.items():
        text = base if fn is None else fn(base)
        if "{{MEMORY_MODEL}}" not in text:
            raise AssertionError(f"variant {name} dropped the {{MEMORY_MODEL}} marker")
        with open(variant_path(name), "w", encoding="utf-8") as f:
            f.write(text)
        written.append(name)
    return written


if __name__ == "__main__":
    base_len = len(control_text())
    for n in build_all():
        chars = len(open(variant_path(n), encoding="utf-8").read())
        print(f"{n:26} {chars:6d} chars  ({chars - base_len:+d} vs control)")
