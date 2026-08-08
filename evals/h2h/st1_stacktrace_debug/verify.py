"""Independent oracle for the salesreport cross-file root-cause bug.

The bug: ``salesreport.fields.normalize_group`` (the DEEPEST frame in the
traceback) canonicalizes a grouping key with ``value.strip().lower()`` and so
crashes on a non-string group value (a numeric region code) or a missing one
(None) -- two frames below the public ``summarize`` call that surfaces it.

A correct fix lives in the shared canonicalizer (fields.py) so EVERY caller is
repaired. This oracle:

  (a) imports the agent's (possibly-edited) package in a FRESH subprocess
      (sys.executable -B) on DATA THE AGENT NEVER SAW;
  (b) asserts the previously-failing operation (summarize) now succeeds on a
      grouping column that mixes strings, an int code, a float code, and None,
      bucketing each correctly;
  (c) REGRESSION GUARDS: the canonicalization (trim + lowercase) still collapses
      whitespace/case variants; numeric VALUE summation is still correct; and
      the RELATED working operations -- leaderboard(...) and the lower-level
      aggregate.distinct_groups(...) -- also work on the same mixed data, so a
      band-aid placed only at the group_sum call site (the single op in the
      traceback) is caught because distinct_groups would still crash;
  (d) asserts the shared helper itself, fields.normalize_group, tolerates a
      non-string and None directly (proves the fix is in the shared layer, not a
      per-call-site special case).

Robustness: any child crash -> (False, detail); the function never raises.
"""
import json
import os
import subprocess
import sys


# Child program. Drives the PUBLIC api AND the lower-level helpers on inputs
# defined HERE (the agent never saw these rows/labels). Emits a JSON blob.
_CHILD = r'''
import json, sys, traceback
sys.path.insert(0, {workdir!r})

out = {{"errors": []}}

def guard(tag, fn):
    try:
        out[tag] = {{"ok": True, "val": fn()}}
    except SystemExit as e:
        out[tag] = {{"ok": False, "err": "SystemExit:%s" % (e.code,)}}
        out["errors"].append("%s: SystemExit" % tag)
    except BaseException as e:
        out[tag] = {{"ok": False, "err": "%s: %s" % (type(e).__name__, e)}}
        out["errors"].append("%s: %s: %s" % (tag, type(e).__name__, e))

from salesreport import summarize, leaderboard
from salesreport.aggregate import distinct_groups
from salesreport.fields import normalize_group

# Unseen dataset: the grouping column mixes textual names (with whitespace/case
# variants that must collapse), an INT region code, a FLOAT region code, and a
# missing (None) cell. Values are numeric strings, ints, and floats.
ROWS = [
    {{"region": "East",   "amount": "10"}},
    {{"region": " east ", "amount": 5}},        # collapses with 'East'
    {{"region": "EAST",   "amount": 2.5}},      # collapses with 'East'
    {{"region": 42,       "amount": "100"}},    # int code -> "42"
    {{"region": 42,       "amount": 8}},        # same int code, accumulates
    {{"region": 3.0,      "amount": "1"}},      # float code -> "3.0"
    {{"region": None,     "amount": "7"}},      # missing -> empty-string group
    {{"region": None,     "amount": 3}},        # same missing bucket, accumulates
    {{"region": "West",   "amount": "bad"}},    # unparseable amount -> +0.0
]

# (b) the previously-failing operation must now succeed.
guard("summary", lambda: summarize(ROWS, "region", "amount"))

# (c) related working op leaderboard must also succeed on the SAME mixed data.
guard("leader", lambda: leaderboard(ROWS, "region", "amount", 3))

# (c) lower-level distinct_groups: a fix scoped only to group_sum leaves THIS
# crashing on the int/None group values.
guard("distinct", lambda: distinct_groups(ROWS, "region"))

# (d) the shared helper must tolerate a non-string and None directly.
guard("norm_int", lambda: normalize_group(42))
guard("norm_none", lambda: normalize_group(None))
guard("norm_float", lambda: normalize_group(3.0))
guard("norm_str", lambda: normalize_group("  North "))   # regression: still trims+lowers

print("JSON_START")
print(json.dumps(out, default=str))
'''


def _run_child(workdir):
    script = _CHILD.format(workdir=workdir)
    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=workdir, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return None, "child timed out after 60s"
    except Exception as e:  # noqa: BLE001
        return None, "could not launch child: %s" % (e,)
    if proc.returncode != 0:
        return None, "child crashed (rc=%d): %s" % (
            proc.returncode, (proc.stderr or proc.stdout)[-600:])
    out = proc.stdout
    if "JSON_START" not in out:
        return None, "child produced no JSON marker:\n" + out[-600:]
    blob = out.split("JSON_START", 1)[1].strip()
    try:
        return json.loads(blob), ""
    except Exception as e:  # noqa: BLE001
        return None, "could not parse child JSON (%r): %s" % (e, blob[-400:])


def _drop_pyc(workdir):
    """Remove cached salesreport bytecode so we always import the CURRENT source
    (an edit landing in the same wall-clock second can leave a stale .pyc)."""
    pkg = os.path.join(workdir, "salesreport")
    pycache = os.path.join(pkg, "__pycache__")
    if os.path.isdir(pycache):
        for fn in os.listdir(pycache):
            if fn.endswith(".pyc"):
                try:
                    os.remove(os.path.join(pycache, fn))
                except OSError:
                    pass


def verify(workdir):
    pkg = os.path.join(workdir, "salesreport")
    if not os.path.isdir(pkg):
        return False, "salesreport package not found in workdir"
    for rel in ("fields.py", "aggregate.py", "report.py"):
        if not os.path.isfile(os.path.join(pkg, rel)):
            return False, "salesreport/%s missing" % rel

    # repro.py must not have been edited away (the task says don't change it).
    if not os.path.isfile(os.path.join(workdir, "repro.py")):
        return False, "repro.py was removed (must not be changed)"

    _drop_pyc(workdir)

    data, err = _run_child(workdir)
    if data is None:
        return False, err

    # Anything that crashed in the child is a hard fail, named explicitly.
    if data.get("errors"):
        return False, "child probe failed: " + " | ".join(data["errors"])[:300]

    # --- (b) summarize must succeed and bucket correctly -----------------------
    s = data.get("summary")
    if not (s and s.get("ok")):
        return False, "summarize() did not return a value: %r" % (s,)
    summ = s["val"]
    if not isinstance(summ, dict):
        return False, "summarize() must return a dict, got %r" % (type(summ).__name__,)

    # Expected canonical buckets and totals on the UNSEEN data:
    #   'east' : 10 + 5 + 2.5            = 17.5   (whitespace/case collapsed)
    #   '42'   : 100 + 8                 = 108.0  (int code stringified)
    #   '3.0'  : 1                       = 1.0    (float code stringified)
    #   ''     : 7 + 3                   = 10.0   (None -> empty-string group)
    #   'west' : +0.0 (unparseable amt)  = 0.0
    expected = {"east": 17.5, "42": 108.0, "3.0": 1.0, "": 10.0, "west": 0.0}
    if set(summ.keys()) != set(expected.keys()):
        return False, ("summarize() buckets wrong: got keys %r, expected %r "
                       "(numeric code -> its text form, None -> empty group, "
                       "whitespace/case collapsed)" % (
                           sorted(summ.keys()), sorted(expected.keys())))
    for k, want in expected.items():
        got = summ.get(k)
        try:
            gotf = float(got)
        except (TypeError, ValueError):
            return False, "summarize()[%r] is non-numeric: %r" % (k, got)
        if abs(gotf - want) > 1e-9:
            return False, "summarize()[%r] = %r, expected %r" % (k, got, want)

    # --- (d) shared helper tolerates non-str / None DIRECTLY -------------------
    if data["norm_int"]["val"] != "42":
        return False, ("normalize_group(42) must be '42', got %r -- the fix must "
                       "be in the shared canonicalizer, not just at one call site"
                       % (data["norm_int"]["val"],))
    if data["norm_none"]["val"] != "":
        return False, ("normalize_group(None) must be '' (empty group), got %r"
                       % (data["norm_none"]["val"],))
    if data["norm_float"]["val"] != "3.0":
        return False, "normalize_group(3.0) must be '3.0', got %r" % (data["norm_float"]["val"],)
    # regression: string canonicalization unchanged (trim + lowercase).
    if data["norm_str"]["val"] != "north":
        return False, ("regression: normalize_group('  North ') must stay 'north' "
                       "(trim+lowercase), got %r" % (data["norm_str"]["val"],))

    # --- (c) related working op distinct_groups on the SAME mixed data ---------
    dg = data["distinct"]["val"]
    if not isinstance(dg, list):
        return False, "distinct_groups() must return a list, got %r" % (type(dg).__name__,)
    if sorted(dg) != ["", "3.0", "42", "east", "west"]:
        return False, ("distinct_groups() labels wrong: %r (a fix scoped only to "
                       "the summarize/group_sum path leaves this crashing on the "
                       "int/None group values)" % (sorted(dg),))

    # --- (c) leaderboard ordering over canonical labels ------------------------
    lb = data["leader"]["val"]
    # list of [label, total]; top 3 by total desc, ties by label asc.
    if not isinstance(lb, list) or len(lb) != 3:
        return False, "leaderboard(top=3) must return 3 rows, got %r" % (lb,)
    norm_lb = [(row[0], float(row[1])) for row in lb]
    expect_lb = [("42", 108.0), ("east", 17.5), ("", 10.0)]
    if norm_lb != expect_lb:
        return False, ("leaderboard(top=3) = %r, expected %r" % (norm_lb, expect_lb))

    return True, ("root cause fixed in the shared canonicalizer: summarize / "
                  "leaderboard / distinct_groups all bucket mixed string/int/"
                  "float/None group values correctly on unseen data, and the "
                  "trim+lowercase canonicalization regression is intact")
