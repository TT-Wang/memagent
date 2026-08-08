"""Independent oracle for the matexpr multi-printer sign-printing fix.

Ports the FAIL_TO_PASS assertions of sympy issue #14237 / PR #14248: a
difference of matrix expressions, internally MatAdd(A, MatMul(-1, B)), must
PRINT with subtraction ('A - B'), and a MatMul with a leading negative
coefficient (MatMul(-1, A, B)) must PRINT as '-A*B', NOT '(-1)*A*B'. The same
contract must hold across THREE printers (str / latex / pretty).

This file is never edited by the agent. The behavioral portion imports the
real package and drives it with expressions DEFINED HERE. It also asserts:
  (A) all three text printers satisfy the contract (consistently);
  (B) the C code printer (distractor) is byte-identical to its seed and still
      renders the (-1) coefficient through the helper-call form;
  (C) old behavior that should be preserved (plain products / plain sums)
      still works (PASS_TO_PASS style).
"""
import ast
import os
import re
import subprocess
import sys


def _pkg(workdir):
    return os.path.join(workdir, "matexpr")


def _read(path):
    with open(path) as f:
        return f.read()


# ---------------------------------------------------------------------------
# Behavioral test: run in a subprocess against the real package with
# expressions constructed right here in the verifier.
# ---------------------------------------------------------------------------
_BEHAVIOR = r'''
import sys, json
sys.path.insert(0, {workdir!r})

from matexpr import MatrixSymbol, MatMul, MatAdd
from matexpr import sstr, latex, pretty, ccode

A = MatrixSymbol("A")
B = MatrixSymbol("B")
C = MatrixSymbol("C")

out = {{}}

# The canonical issue expression: A - A*B - B
# internally MatAdd(A, MatMul(-1, A, B), MatMul(-1, B))
e1 = A - A * B - B
out["str_e1"] = sstr(e1)
out["latex_e1"] = latex(e1)
out["pretty_e1"] = pretty(e1)

# A bare negated product: -A*B  ==  MatMul(-1, A, B)
e2 = MatMul(-1, A, B)
out["str_e2"] = sstr(e2)
out["latex_e2"] = latex(e2)
out["pretty_e2"] = pretty(e2)

# A bare negation: -A  == MatMul(-1, A)
e3 = -A
out["str_e3"] = sstr(e3)
out["latex_e3"] = latex(e3)
out["pretty_e3"] = pretty(e3)

# Leading negative term in a sum: -A + B
e4 = MatAdd(MatMul(-1, A), B)
out["str_e4"] = sstr(e4)
out["latex_e4"] = latex(e4)
out["pretty_e4"] = pretty(e4)

# A three-way mixed sum: A - B + C  (plus/minus interleaved)
e5 = A - B + C
out["str_e5"] = sstr(e5)
out["latex_e5"] = latex(e5)
out["pretty_e5"] = pretty(e5)

# --- PASS_TO_PASS: plain (all-positive) cases must be unaffected ---
e6 = A + B          # plain sum
e7 = A * B          # plain product
out["str_e6"] = sstr(e6)
out["latex_e6"] = latex(e6)
out["pretty_e6"] = pretty(e6)
out["str_e7"] = sstr(e7)
out["latex_e7"] = latex(e7)
out["pretty_e7"] = pretty(e7)

# --- DISTRACTOR: the C code printer must keep rendering the helper-call form
# (including the literal -1 coefficient). It is NOT part of the sign contract.
out["ccode_e1"] = ccode(e1)
out["ccode_e2"] = ccode(e2)

print("JSON_START")
print(json.dumps(out))
'''


def _run_behavior(workdir):
    script = _BEHAVIOR.format(workdir=workdir)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return None, "behavioral test raised:\n" + (proc.stderr or proc.stdout)
    out = proc.stdout
    if "JSON_START" not in out:
        return None, "behavioral test produced no JSON:\n" + out
    import json
    blob = out.split("JSON_START", 1)[1].strip()
    try:
        return json.loads(blob), ""
    except Exception as e:  # noqa: BLE001
        return None, "could not parse behavioral JSON (%r):\n%s" % (e, blob)


DOT = "⋅"


def _check_behavior(workdir):
    data, err = _run_behavior(workdir)
    if data is None:
        return False, err

    # ----- (A) the sign/subtraction contract, per printer -----
    # str printer: A - A*B - B
    expected = {
        "str_e1": "A - A*B - B",
        "str_e2": "-A*B",
        "str_e3": "-A",
        "str_e4": "-A + B",
        "str_e5": "A - B + C",
        "latex_e1": "A - A B - B",
        "latex_e2": "-A B",
        "latex_e3": "-A",
        "latex_e4": "-A + B",
        "latex_e5": "A - B + C",
        "pretty_e1": "A - A%sB - B" % DOT,
        "pretty_e2": "-A%sB" % DOT,
        "pretty_e3": "-A",
        "pretty_e4": "-A + B",
        "pretty_e5": "A - B + C",
        # PASS_TO_PASS: plain cases unchanged.
        "str_e6": "A + B",
        "str_e7": "A*B",
        "latex_e6": "A + B",
        "latex_e7": "A B",
        "pretty_e6": "A + B",
        "pretty_e7": "A%sB" % DOT,
    }
    for key, want in expected.items():
        got = data.get(key)
        if got != want:
            printer = key.split("_")[0]
            return False, ("%s printer wrong for %s: expected %r, got %r "
                           "(the MatAdd/MatMul sign contract must be applied "
                           "in %s_printer.py)" % (printer, key, want, got, printer))

    # Cross-printer consistency: none of the three text printers may leak the
    # internal '(-1)' coefficient anywhere.
    for key, val in data.items():
        if key.startswith(("str_", "latex_", "pretty_")):
            if "(-1)" in val or "(-1" in val or "-1" in val.replace("-1)", "X"):
                # allow nothing containing a literal -1 in text printers
                if "-1" in val:
                    return False, ("%s leaked the internal coefficient: %r" % (key, val))

    # ----- (C) distractor: code printer must keep helper-call form + (-1) -----
    if data.get("ccode_e1") != "matadd(A, matmul(-1, A, B), matmul(-1, B))":
        return False, ("C code printer must stay unchanged (helper-call form); "
                       "got %r" % (data.get("ccode_e1"),))
    if data.get("ccode_e2") != "matmul(-1, A, B)":
        return False, ("C code printer must stay unchanged; got %r"
                       % (data.get("ccode_e2"),))

    return True, "behavior OK"


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------
def _check_distractor_unchanged(workdir):
    """The C code printer must be BYTE-IDENTICAL to its seed.

    We reconstruct the seed content from setup.py's writer and compare.
    """
    # Re-derive the canonical seed for code_printer.py by importing setup and
    # writing to a scratch dir, then comparing bytes.
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import importlib
    setup_mod = importlib.import_module("setup")
    importlib.reload(setup_mod)

    scratch = tempfile.mkdtemp(prefix="m5_seed_")
    setup_mod.setup(scratch)
    seed_path = os.path.join(scratch, "matexpr", "code_printer.py")
    cur_path = os.path.join(_pkg(workdir), "code_printer.py")
    if not os.path.exists(cur_path):
        return False, "distractor code_printer.py was removed"
    if _read(seed_path) != _read(cur_path):
        return False, ("distractor matexpr/code_printer.py was modified; it "
                       "must stay byte-identical (the C printer is NOT part of "
                       "the sign/subtraction contract)")

    # version.py distractor too.
    ver = os.path.join(_pkg(workdir), "version.py")
    if not os.path.exists(ver) or '__version__ = "0.3.0"' not in _read(ver):
        return False, "distractor version.py was modified or removed"
    return True, "distractors unchanged"


def _check_all_three_changed(workdir):
    """Sanity: every text printer must have actually adopted the contract.

    Each fixed printer must (a) no longer print a parenthesized negative
    coefficient via the old '(%d)' form for MatMul, and (b) contain logic that
    emits a '-' sign. We check the source for the old buggy markers being gone
    and a sign branch being present, in all three files.
    """
    pkg = _pkg(workdir)
    targets = {
        "str_printer.py": os.path.join(pkg, "str_printer.py"),
        "latex_printer.py": os.path.join(pkg, "latex_printer.py"),
        "pretty_printer.py": os.path.join(pkg, "pretty_printer.py"),
    }
    old_marker = re.compile(r'"\(%d\)"\s*%')   # the buggy "(%d)" % arg.value
    for label, path in targets.items():
        if not os.path.exists(path):
            return False, "%s is missing" % (label,)
        src = _read(path)
        if old_marker.search(src):
            return False, ("%s still prints the raw negative coefficient via "
                           "the old '(%%d)' form; the MatMul sign fix was not "
                           "applied here" % (label,))
        if "-" not in src:
            return False, "%s has no '-' sign handling" % (label,)
    return True, "all three printers updated"


def _check_syntax(workdir):
    pkg = _pkg(workdir)
    for root, _dirs, files in os.walk(pkg):
        for fn in files:
            if fn.endswith(".py"):
                p = os.path.join(root, fn)
                try:
                    ast.parse(_read(p))
                except SyntaxError as e:
                    return False, "syntax error in %s: %s" % (
                        os.path.relpath(p, workdir), e)
    return True, "syntax ok"


def verify(workdir):
    checks = [
        ("syntax", _check_syntax),
        ("distractor_unchanged", _check_distractor_unchanged),
        ("all_three_changed", _check_all_three_changed),
        ("behavior", _check_behavior),
    ]
    for name, fn in checks:
        ok, detail = fn(workdir)
        if not ok:
            return False, "[%s] %s" % (name, detail)
    return True, ("all checks passed: MatAdd/MatMul sign printing fixed "
                  "consistently across str/latex/pretty; code printer untouched")
