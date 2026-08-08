import os
import sys
import json
import subprocess


# Independent oracle. We do NOT trust any test the agent can see. We import the
# agent's FINAL calc.py in a FRESH subprocess (sys.executable -B) and exercise
# the public API (evaluate / Calculator) on EXPRESSIONS THE AGENT NEVER SAW --
# different numbers and shapes than the prompts/README -- asserting real numeric
# results AND that malformed inputs raise the project's CalcError (not raw Python
# errors). Includes REGRESSION GUARDS so a late turn cannot silently break an
# earlier operator. A child crash returns (False, detail), never an exception.


# ---- VALUE cases: (expr, env_or_None, expected_number). All UNSEEN inputs. ----
# Computed expectations are spelled out as literals so the oracle does not just
# re-run Python's eval(); they double as the spec.
_VALUE = [
    # turn 1: literals + - * / and precedence/associativity (unseen numbers)
    ("8 + 6 * 2", None, 20),
    ("100 - 30 - 20", None, 50),
    ("9 / 4", None, 2.25),
    ("1 + 2 + 3 + 4", None, 10),
    ("2.5 * 4", None, 10.0),
    # turn 2: parentheses + nesting
    ("3 * (2 + 5)", None, 21),
    ("((8 - 3) * (2 + 2))", None, 20),
    ("(2 + 3) * (4 + 1) - 5", None, 20),
    # turn 3: // % ** and their precedence
    ("20 // 6", None, 3),
    ("20 % 6", None, 2),
    ("100 // 7 % 4", None, 2),          # (100//7)=14, 14%4=2
    ("3 * 4 ** 2", None, 48),          # ** before *  -> 3*16
    ("2 ** 4 + 1", None, 17),
    # turn 4: unary +/-
    ("-7", None, -7),
    ("10 + -3", None, 7),
    ("-(4 + 6)", None, -10),
    ("---5", None, -5),
    ("-3 * 4", None, -12),
    ("8 - -2", None, 10),
    # turn 5: right-assoc ** and unary/power interaction (the trap)
    ("2 ** 2 ** 3", None, 256),        # 2 ** (2**3) = 2**8
    ("-3 ** 2", None, -9),             # -(3**2)
    ("2 ** -2", None, 0.25),
    ("-2 ** -2", None, -0.25),
    ("4 ** 0.5", None, 2.0),
    # turn 6: variables + assignment + persistence (env form)
    ("a * b", {"a": 6, "b": 7}, 42),
    ("n + 1", {"n": 41}, 42),
    ("k ** 2 + 1", {"k": 5}, 26),
    # turn 7: built-in function calls (unseen args)
    ("abs(-9)", None, 9),
    ("abs(3 - 10)", None, 7),
    ("pow(2, 10)", None, 1024),
    ("max(4, 9, 2, 7)", None, 9),
    ("min(4, 9, 2, 7)", None, 2),
    ("sqrt(144)", None, 12.0),
    ("sqrt(5 ** 2 + 12 ** 2)", None, 13.0),
    ("max(1 + 1, 3, 2) * 2", None, 6),
    ("pow(abs(-2), 5)", None, 32),     # nested calls
    ("max(2, min(10, 3))", None, 3),
    # cross-feature: vars + funcs + power + parens together
    ("sqrt(r * r)", {"r": 5}, 5.0),
    ("pow(x, 2) + pow(y, 2)", {"x": 3, "y": 4}, 25),
]


# ---- ASSIGNMENT-PERSISTENCE script: a sequence of .eval() lines on ONE
#      Calculator instance, with expected return per line (None for assigns). --
_PERSIST = [
    ("p = 10", None),
    ("q = p * 2", None),       # rhs references earlier var
    ("p + q", 30),
    ("p = p + 5", None),       # reassign using own value
    ("p", 15),
    ("q", 20),
    ("r = pow(p - 5, 2)", None),  # assignment rhs uses a function + var
    ("r", 100),
]


# ---- ERROR cases: (expr, env_or_None) that MUST raise CalcError. UNSEEN. ----
_ERRORS = [
    ("5 / 0", None),
    ("9 // 0", None),
    ("9 % 0", None),
    ("zzz", None),                 # unknown variable
    ("a + missing", {"a": 1}),     # one known, one unknown var
    ("bogus(3)", None),            # unknown function
    ("abs(1, 2)", None),           # wrong arity (too many)
    ("pow(2)", None),              # wrong arity (too few)
    ("min(5)", None),              # min needs >= 2
    ("max()", None),               # max needs >= 2
    ("(3 + 4", None),              # unmatched open paren
    ("3 + 4)", None),              # stray close paren
    ("4 5", None),                 # leftover token
    ("6 +", None),                 # dangling operator
    ("* 8", None),                 # leading binary op
    ("", None),                    # empty
    ("    ", None),                # whitespace only
    ("sqrt(-4)", None),            # math domain -> must be CalcError
]


_CHILD = r'''
import json, sys, math
import calc

out = {"value": [], "persist": [], "error": [], "meta": {}}

# CalcError must exist and subclass Exception.
CE = getattr(calc, "CalcError", None)
out["meta"]["has_calcerror"] = bool(isinstance(CE, type) and issubclass(CE, Exception))
out["meta"]["has_evaluate"] = hasattr(calc, "evaluate")
out["meta"]["has_calculator"] = hasattr(calc, "Calculator")

VALUE = json.loads(sys.argv[1])
PERSIST = json.loads(sys.argv[2])
ERRORS = json.loads(sys.argv[3])


def _num(x):
    # JSON-safe numeric record (so the parent can compare with tolerance).
    try:
        return {"ok": True, "v": float(x), "is_num": isinstance(x, (int, float)) and not isinstance(x, bool)}
    except Exception as e:
        return {"ok": False, "err": "%s:%s" % (type(e).__name__, e)}


# --- value cases via evaluate() ---
for expr, env in VALUE:
    try:
        r = calc.evaluate(expr) if env is None else calc.evaluate(expr, env)
        out["value"].append(_num(r))
    except BaseException as e:
        out["value"].append({"ok": False, "err": "%s:%s" % (type(e).__name__, e)})

# --- persistence: ONE Calculator instance across many .eval() lines ---
try:
    c = calc.Calculator()
    for line in PERSIST:
        try:
            r = c.eval(line)
            if r is None:
                out["persist"].append({"ok": True, "none": True})
            else:
                rec = _num(r); rec["none"] = False
                out["persist"].append(rec)
        except BaseException as e:
            out["persist"].append({"ok": False, "err": "%s:%s" % (type(e).__name__, e)})
except BaseException as e:
    out["persist"].append({"ok": False, "err": "ctor:%s:%s" % (type(e).__name__, e)})

# --- error cases: must raise calc.CalcError specifically ---
for expr, env in ERRORS:
    try:
        r = calc.evaluate(expr) if env is None else calc.evaluate(expr, env)
        out["error"].append({"raised": False, "ret": repr(r)[:60]})
    except BaseException as e:
        is_calcerr = CE is not None and isinstance(e, CE)
        out["error"].append({"raised": True, "is_calcerr": bool(is_calcerr),
                             "type": type(e).__name__, "msg": str(e)[:80]})

sys.stdout.write(json.dumps(out))
'''


def _approx(a, b, tol=1e-9):
    try:
        return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))
    except Exception:
        return False


def verify(workdir):
    calc_path = os.path.join(workdir, "calc.py")
    if not os.path.isfile(calc_path):
        return False, "calc.py not found in workdir"

    # Drop any stale cached bytecode so we always test the CURRENT source.
    pycache = os.path.join(workdir, "__pycache__")
    if os.path.isdir(pycache):
        for fn in os.listdir(pycache):
            if fn.startswith("calc.") and fn.endswith(".pyc"):
                try:
                    os.remove(os.path.join(pycache, fn))
                except OSError:
                    pass

    # The child only needs the INPUTS (expr/env, or the assignment lines), never
    # the expected results -- the parent owns the comparison so the agent's code
    # can't read the answers from anywhere it sees.
    value_in = [[expr, env] for (expr, env, _expected) in _VALUE]
    persist_in = [line for (line, _expected) in _PERSIST]
    errors_in = [[expr, env] for (expr, env) in _ERRORS]
    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-c", _CHILD,
             json.dumps(value_in), json.dumps(persist_in), json.dumps(errors_in)],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "child timed out (possible infinite loop in parser)"
    except Exception as e:  # noqa: BLE001
        return False, "could not launch child: %s" % e

    if proc.returncode != 0:
        return False, "child crashed (rc=%d): %s" % (
            proc.returncode, (proc.stderr or proc.stdout)[-500:])
    try:
        out = json.loads(proc.stdout)
    except Exception as e:  # noqa: BLE001
        return False, "could not parse child output: %s :: %r" % (e, proc.stdout[-300:])

    meta = out.get("meta", {})
    if not meta.get("has_evaluate"):
        return False, "calc.py has no module-level evaluate()"
    if not meta.get("has_calculator"):
        return False, "calc.py has no Calculator class"
    if not meta.get("has_calcerror"):
        return False, "calc.py has no CalcError(Exception) class"

    # --- value cases ---
    vres = out.get("value", [])
    if len(vres) != len(_VALUE):
        return False, "value-case count mismatch (%d vs %d)" % (len(vres), len(_VALUE))
    for (expr, _env, expected), got in zip(_VALUE, vres):
        if not got.get("ok"):
            return False, "FAIL value %r -> error %s" % (expr, got.get("err"))
        if not got.get("is_num"):
            return False, "FAIL value %r -> non-numeric result" % (expr,)
        if not _approx(got.get("v"), expected):
            return False, "FAIL value %r -> %r, expected %r" % (expr, got.get("v"), expected)

    # --- persistence script ---
    pres = out.get("persist", [])
    if len(pres) != len(_PERSIST):
        return False, "persist-step count mismatch (%d vs %d)" % (len(pres), len(_PERSIST))
    for (line, expected), got in zip(_PERSIST, pres):
        if not got.get("ok"):
            return False, "FAIL persist %r -> error %s" % (line, got.get("err"))
        if expected is None:
            if not got.get("none"):
                return False, "FAIL persist %r -> %r, expected None (assignment)" % (
                    line, got.get("v"))
        else:
            if got.get("none"):
                return False, "FAIL persist %r -> None, expected %r" % (line, expected)
            if not _approx(got.get("v"), expected):
                return False, "FAIL persist %r -> %r, expected %r" % (
                    line, got.get("v"), expected)

    # --- error cases: must raise CalcError specifically ---
    eres = out.get("error", [])
    if len(eres) != len(_ERRORS):
        return False, "error-case count mismatch (%d vs %d)" % (len(eres), len(_ERRORS))
    for (expr, _env), got in zip(_ERRORS, eres):
        if not got.get("raised"):
            return False, "FAIL error %r -> did NOT raise (returned %s)" % (
                expr, got.get("ret"))
        if not got.get("is_calcerr"):
            return False, "FAIL error %r -> raised %s (not CalcError): %s" % (
                expr, got.get("type"), got.get("msg"))

    return True, ("all %d value cases + %d persistence steps + %d error cases pass "
                  "(precedence, right-assoc **, unary, parens, vars, funcs, uniform "
                  "CalcError -- all on unseen inputs)") % (
        len(_VALUE), len(_PERSIST), len(_ERRORS))
