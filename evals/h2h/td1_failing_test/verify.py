import os
import sys
import json
import subprocess


# Independent oracle. It does TWO things in FRESH subprocesses, never importing
# anything into this process and never trusting a test the agent can edit:
#
#   (1) Runs the WHOLE unittest suite (test_split.py) exactly as shipped, in a
#       fresh `python -B -m unittest` subprocess, and requires it ALL green.
#       This is the regression guard: the agent must not have weakened or
#       deleted the other tests to get the failing one to pass, and must not
#       have broken any passing test.
#
#   (2) Imports the (possibly-edited) allocator.py in a SEPARATE fresh child and
#       hammers allocate() with inputs the agent NEVER SAW (uneven splits,
#       weighted splits, larger totals), asserting the exact largest-remainder
#       result AND the invariants (sum preserved, no negative share, no share
#       off by more than one cent from its floor). These cannot be satisfied by
#       hard-coding the test file's examples or by hacking the ledger layer.


# ---- child #2: unseen allocator behavior, emitted as JSON ------------------
_ALLOC_CHILD = r'''
import json, sys
from allocator import allocate, AllocationError

out = {}

def grab(tag, total, weights):
    try:
        out[tag] = {"ok": True, "shares": list(allocate(total, weights))}
    except BaseException as e:
        out[tag] = {"ok": False, "err": "%s:%s" % (type(e).__name__, e)}

# Unseen uneven splits (none appear in test_split.py).
grab("u_1000_3eq",  1000, [1, 1, 1])         # 334,333,333
grab("u_100_211",   100,  [2, 1, 1])         # even at 50/25/25
grab("u_103_4eq",   103,  [1, 1, 1, 1])      # 26,26,26,25
grab("u_777_532",   777,  [5, 3, 2])         # weighted, leftover spread
grab("u_10_3eq",    10,   [1, 1, 1])         # 4,3,3
grab("u_1_3eq",     1,    [1, 1, 1])         # single penny -> first
grab("u_5_2eq",     5,    [1, 1])            # 3,2

# Validation must still reject bad input (regression).
try:
    allocate(100, [])
    out["rej_empty"] = {"raised": False}
except AllocationError:
    out["rej_empty"] = {"raised": True}
except BaseException as e:
    out["rej_empty"] = {"raised": False, "wrong": "%s" % type(e).__name__}

try:
    allocate(50, [0, 0, 0])
    out["rej_zerosum"] = {"raised": False}
except AllocationError:
    out["rej_zerosum"] = {"raised": True}
except BaseException as e:
    out["rej_zerosum"] = {"raised": False, "wrong": "%s" % type(e).__name__}

sys.stdout.write(json.dumps(out))
'''


def _expected(total, weights):
    """Reference largest-remainder allocation, computed here in the oracle so
    the check is independent of the agent's implementation."""
    s = sum(weights)
    shares, rem = [], []
    for w in weights:
        q, r = divmod(total * w, s)
        shares.append(q)
        rem.append(r)
    leftover = total - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-rem[i], i))
    for k in range(leftover):
        shares[order[k]] += 1
    return shares


# (tag, total, weights) for the unseen cases the child runs.
_UNSEEN = [
    ("u_1000_3eq", 1000, [1, 1, 1]),
    ("u_100_211", 100, [2, 1, 1]),
    ("u_103_4eq", 103, [1, 1, 1, 1]),
    ("u_777_532", 777, [5, 3, 2]),
    ("u_10_3eq", 10, [1, 1, 1]),
    ("u_1_3eq", 1, [1, 1, 1]),
    ("u_5_2eq", 5, [1, 1]),
]


def _drop_pyc(workdir):
    """Drop cached bytecode so we always test the CURRENT source (an edit landing
    in the same wall-clock second can otherwise leave a stale .pyc)."""
    pycache = os.path.join(workdir, "__pycache__")
    if os.path.isdir(pycache):
        for fn in os.listdir(pycache):
            if fn.endswith(".pyc"):
                try:
                    os.remove(os.path.join(pycache, fn))
                except OSError:
                    pass


def verify(workdir):
    for required in ("allocator.py", "money.py", "ledger.py", "test_split.py"):
        if not os.path.isfile(os.path.join(workdir, required)):
            return False, "%s not found in workdir" % required

    _drop_pyc(workdir)

    # --- (1) WHOLE unittest suite, fresh subprocess, must be all green --------
    try:
        suite = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "-v", "test_split"],
            cwd=workdir, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "unittest suite timed out (possible infinite loop)"
    except BaseException as e:
        return False, "could not launch unittest suite: %s" % e

    combined = (suite.stderr or "") + (suite.stdout or "")
    if suite.returncode != 0:
        return False, "unittest suite is NOT all green:\n" + combined[-900:]
    # unittest prints a trailing "OK"; guard against an empty/zero-test run.
    if "OK" not in combined:
        return False, "unittest suite did not report OK:\n" + combined[-900:]
    # The suite must still contain the named pinning test (not deleted/renamed).
    if "test_leftover_pennies_are_fair" not in combined:
        return False, ("the pinning test test_leftover_pennies_are_fair is "
                       "missing from the run -- it must not be deleted:\n"
                       + combined[-700:])

    # --- (2) UNSEEN allocator behavior, separate fresh subprocess ------------
    _drop_pyc(workdir)
    try:
        child = subprocess.run(
            [sys.executable, "-B", "-c", _ALLOC_CHILD],
            cwd=workdir, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "allocator oracle child timed out"
    except BaseException as e:
        return False, "could not launch allocator oracle: %s" % e

    if child.returncode != 0:
        return False, "allocator oracle child crashed (rc=%d): %s" % (
            child.returncode, (child.stderr or child.stdout)[-600:])
    try:
        res = json.loads(child.stdout)
    except Exception as e:
        return False, "could not parse oracle output: %s :: %r" % (
            e, child.stdout[-400:])

    # Exact largest-remainder results on inputs the agent never saw.
    for tag, total, weights in _UNSEEN:
        r = res.get(tag)
        if r is None:
            return False, "missing unseen case %r" % tag
        if not r.get("ok"):
            return False, "unseen case %r raised: %s" % (tag, r.get("err"))
        got = r["shares"]
        exp = _expected(total, weights)
        if got != exp:
            return False, "unseen case %r: got %r, expected %r" % (tag, got, exp)
        # Invariants, independent of the exact tie-break:
        if sum(got) != total:
            return False, "unseen case %r: shares sum to %d, not %d" % (
                tag, sum(got), total)
        if any(x < 0 for x in got):
            return False, "unseen case %r: negative share in %r" % (tag, got)
        s = sum(weights)
        for w, x in zip(weights, got):
            floor = total * w // s
            if x - floor not in (0, 1):
                return False, ("unseen case %r: share %d is more than one cent "
                               "off its floor %d" % (tag, x, floor))

    # Validation regressions.
    if not res.get("rej_empty", {}).get("raised"):
        return False, "allocate([]) must raise AllocationError (got %r)" % (
            res.get("rej_empty"),)
    if not res.get("rej_zerosum", {}).get("raised"):
        return False, "allocate(zero weights) must raise AllocationError (got %r)" % (
            res.get("rej_zerosum"),)

    return True, ("whole unittest suite green AND 7 unseen uneven/weighted "
                  "splits match largest-remainder (sum preserved, off-by-<=1 "
                  "from floor) with validation intact")
