"""Independent oracle for the httpretry RATE_LIMITED consistency bug fix.

The bug: the RATE_LIMITED contract (status codes 429 and 413 are throttling
responses that should be retried gently and whose Retry-After header must be
honored) is split across four modules and was only half-wired. A correct fix
must touch all four consistently:

  * status.py     -- classify(429) / classify(413) -> RetryClass.RATE_LIMITED
  * policy.py     -- is_retryable treats RATE_LIMITED as retryable
  * backoff.py    -- _BASE_DELAY has a RATE_LIMITED entry (and it backs off
                     MORE than TRANSIENT)
  * retryafter.py -- honors_retry_after is True for RATE_LIMITED codes

This oracle:
  (a) drives client.py (which the task does NOT ask the agent to edit) with
      inputs DEFINED HERE -- a fresh RetryClient and fresh status codes/headers;
  (b) tests BEHAVIOR end-to-end via a subprocess against the real package;
  (c) asserts the change is applied CONSISTENTLY across all four must-change
      files (a partial fix in only some files still FAILS, naming the first
      inconsistency);
  (d) asserts the DISTRACTOR files (statuscodes.py, version.py) are byte-for-byte
      unchanged from the seed.
"""
import hashlib
import os
import subprocess
import sys


def _pkg(workdir):
    return os.path.join(workdir, "httpretry")


def _read(path):
    with open(path) as f:
        return f.read()


# ---------------------------------------------------------------------------
# (d) Distractors must be byte-identical to the seed. We recompute the seed
#     content here (independently of setup.py) and compare hashes, so the
#     check does not depend on importing setup at verify time.
# ---------------------------------------------------------------------------
_SEED_STATUSCODES = '''\
"""HTTP status code reason phrases. DISTRACTOR -- do NOT change for this fix.

This is a passive lookup table mapping numeric codes to their human-readable
reason phrase. It deliberately mentions 429 and 413 and the word "retry" in
prose, but it has nothing to do with the retry-classification contract. Editing
this file (e.g. via a blanket find-replace of 429/413) is incorrect.
"""

# code -> reason phrase. (RFC 9110 reason phrases.)
REASON_PHRASES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    408: "Request Timeout",
    413: "Content Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


def name_for(code):
    """Return the reason phrase for a status code (or 'Unknown')."""
    return REASON_PHRASES.get(code, "Unknown")


def is_client_error(code):
    """True for 4xx codes. Note: a 429 may still be worth a retry elsewhere."""
    return 400 <= code < 500
'''

_SEED_VERSION = '''\
"""Version metadata. DISTRACTOR -- do NOT change for this fix."""

__version__ = "0.3.0"


def version_tuple():
    return tuple(int(p) for p in __version__.split("."))
'''


def _check_distractors_unchanged(workdir):
    pkg = _pkg(workdir)
    expected = {
        "statuscodes.py": _SEED_STATUSCODES,
        "version.py": _SEED_VERSION,
    }
    for rel, seed_src in expected.items():
        path = os.path.join(pkg, rel)
        if not os.path.exists(path):
            return False, "distractor file removed: httpretry/%s" % rel
        actual = _read(path)
        if actual != seed_src:
            want = hashlib.sha256(seed_src.encode()).hexdigest()[:12]
            got = hashlib.sha256(actual.encode()).hexdigest()[:12]
            return False, (
                "distractor httpretry/%s was modified (must stay byte-identical "
                "to the seed; sha seed=%s got=%s)" % (rel, want, got)
            )
    return True, "distractors unchanged"


# ---------------------------------------------------------------------------
# (c) Structural consistency: the four must-change files must each have been
#     made RATE_LIMITED-aware. We check the package, NOT client.py.
# ---------------------------------------------------------------------------
def _check_consistency(workdir):
    pkg = _pkg(workdir)

    # status.py: 429 and 413 must be classified RATE_LIMITED. We do not pin the
    # exact source form, so we verify behaviorally below; here we only ensure
    # the file still defines RetryClass.RATE_LIMITED and classify().
    status_src = _read(os.path.join(pkg, "status.py"))
    if "RATE_LIMITED" not in status_src or "def classify" not in status_src:
        return False, "status.py must keep RetryClass.RATE_LIMITED and classify()"

    # policy.py must reference RATE_LIMITED (cannot be retryable otherwise).
    policy_src = _read(os.path.join(pkg, "policy.py"))
    if "RATE_LIMITED" not in policy_src:
        return False, (
            "policy.py never references RATE_LIMITED -- is_retryable still "
            "drops rate-limited responses"
        )

    # backoff.py must reference RATE_LIMITED in its delay table.
    backoff_src = _read(os.path.join(pkg, "backoff.py"))
    if "RATE_LIMITED" not in backoff_src:
        return False, (
            "backoff.py never references RATE_LIMITED -- _BASE_DELAY has no "
            "rate-limited entry and will KeyError on 429/413"
        )

    # retryafter.py must consult the RATE_LIMITED class (not a hardcoded code
    # list that omits it).
    ra_src = _read(os.path.join(pkg, "retryafter.py"))
    if "RATE_LIMITED" not in ra_src:
        return False, (
            "retryafter.py never references RATE_LIMITED -- 429/413 Retry-After "
            "headers are still ignored"
        )

    return True, "consistency markers present"


# ---------------------------------------------------------------------------
# (a)+(b) Behavioral end-to-end test driven through client.py with inputs
#         defined HERE. Run in a subprocess against the real package.
# ---------------------------------------------------------------------------
_BEHAVIOR = r'''
import sys, json
sys.path.insert(0, {workdir!r})

from httpretry import RetryClass, classify, RetryClient
from httpretry.backoff import delay_for
from httpretry.retryafter import honors_retry_after

out = {{}}
errors = []

# Fresh client constructed HERE (agent never saw this instance).
client = RetryClient()

# --- classification contract ---
out["cls_429"] = classify(429).name
out["cls_413"] = classify(413).name
out["cls_503"] = classify(503).name   # stays TRANSIENT
out["cls_500"] = classify(500).name   # stays TRANSIENT
out["cls_404"] = classify(404).name   # stays PERMANENT

# --- is the rate-limited response retried at all? (catches policy.py) ---
# next_wait returns None iff not retryable / out of attempts.
try:
    nw_429 = client.next_wait(429, attempt=1)
except Exception as e:
    nw_429 = "RAISED:%s" % (e,)
    errors.append("next_wait(429) raised: %r" % (e,))
out["nw_429_attempt1"] = nw_429

try:
    nw_413 = client.next_wait(413, attempt=1)
except Exception as e:
    nw_413 = "RAISED:%s" % (e,)
    errors.append("next_wait(413) raised: %r" % (e,))
out["nw_413_attempt1"] = nw_413

# --- backoff for rate-limited vs transient (catches backoff.py) ---
# Same attempt number; rate-limited must back off MORE than transient, proving
# 429 was wired as its OWN class (not dumped into the transient code set).
try:
    out["delay_429_a2"] = delay_for(429, 2)
except Exception as e:
    out["delay_429_a2"] = "RAISED:%s" % (e,)
    errors.append("delay_for(429,2) raised: %r" % (e,))
try:
    out["delay_500_a2"] = delay_for(500, 2)
except Exception as e:
    out["delay_500_a2"] = "RAISED:%s" % (e,)
    errors.append("delay_for(500,2) raised: %r" % (e,))

# --- Retry-After honoring (catches retryafter.py) ---
out["ra_429"] = honors_retry_after(429)
out["ra_413"] = honors_retry_after(413)
out["ra_503"] = honors_retry_after(503)   # must still be honored
out["ra_500"] = honors_retry_after(500)   # transient w/o Retry-After -> False

# end-to-end: a 429 with Retry-After must return that header value exactly.
try:
    out["nw_429_retryafter"] = client.next_wait(
        429, attempt=1, headers={{"Retry-After": "11"}}
    )
except Exception as e:
    out["nw_429_retryafter"] = "RAISED:%s" % (e,)
    errors.append("next_wait(429, Retry-After) raised: %r" % (e,))

# regression guard: previously-correct behavior must be preserved.
try:
    out["nw_503_retryafter"] = client.next_wait(
        503, attempt=1, headers={{"Retry-After": "7"}}
    )
except Exception as e:
    out["nw_503_retryafter"] = "RAISED:%s" % (e,)
    errors.append("next_wait(503, Retry-After) raised: %r" % (e,))
try:
    out["nw_500_attempt1"] = client.next_wait(500, attempt=1)   # -> 0.5
except Exception as e:
    out["nw_500_attempt1"] = "RAISED:%s" % (e,)
    errors.append("next_wait(500) raised: %r" % (e,))
out["nw_404_attempt1"] = client.next_wait(404, attempt=1)       # -> None

out["errors"] = errors
print("JSON_START")
print(json.dumps(out, default=str))
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


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _check_behavior(workdir):
    data, err = _run_behavior(workdir)
    if data is None:
        return False, err

    # classification (status.py)
    if data.get("cls_429") != "RATE_LIMITED":
        return False, ("status.classify(429) must be RATE_LIMITED, got %r "
                       "(status.py not updated)" % (data.get("cls_429"),))
    if data.get("cls_413") != "RATE_LIMITED":
        return False, ("status.classify(413) must be RATE_LIMITED, got %r "
                       "(status.py not updated)" % (data.get("cls_413"),))
    # the seed's already-correct classifications must be preserved.
    if data.get("cls_503") != "TRANSIENT":
        return False, "regression: classify(503) must stay TRANSIENT, got %r" % (data.get("cls_503"),)
    if data.get("cls_500") != "TRANSIENT":
        return False, "regression: classify(500) must stay TRANSIENT, got %r" % (data.get("cls_500"),)
    if data.get("cls_404") != "PERMANENT":
        return False, "regression: classify(404) must stay PERMANENT, got %r" % (data.get("cls_404"),)

    # retryability (policy.py): 429 & 413 must be retried (non-None wait).
    for code in (429, 413):
        nw = data.get("nw_%d_attempt1" % code)
        if isinstance(nw, str) and nw.startswith("RAISED:"):
            return False, ("client.next_wait(%d) raised -- backoff.py has no "
                           "RATE_LIMITED delay (%s)" % (code, nw))
        if nw is None:
            return False, ("client.next_wait(%d) returned None -- policy.is_retryable "
                           "still drops RATE_LIMITED responses (policy.py not updated)" % (code,))
        if _as_float(nw) is None:
            return False, "client.next_wait(%d) returned a non-numeric wait %r" % (code, nw)

    # backoff (backoff.py): rate-limited must NOT raise and must back off MORE
    # than transient for the same attempt -> proves 429 is its own class, not
    # merely appended to the transient code set.
    d429 = data.get("delay_429_a2")
    if isinstance(d429, str) and d429.startswith("RAISED:"):
        return False, ("backoff.delay_for(429) raised -- _BASE_DELAY missing a "
                       "RATE_LIMITED entry (backoff.py not updated): %s" % (d429,))
    d429f = _as_float(d429)
    d500f = _as_float(data.get("delay_500_a2"))
    if d429f is None or d500f is None:
        return False, "backoff delays must be numeric, got 429=%r 500=%r" % (d429, data.get("delay_500_a2"))
    if not (d429f > d500f):
        return False, ("rate-limited backoff (delay_for(429,2)=%r) must be GREATER "
                       "than transient backoff (delay_for(500,2)=%r): 429 must be "
                       "wired as its own RATE_LIMITED class with a larger base "
                       "delay, not folded into the transient set" % (d429f, d500f))

    # retry-after (retryafter.py)
    if data.get("ra_429") is not True:
        return False, "honors_retry_after(429) must be True (retryafter.py not updated)"
    if data.get("ra_413") is not True:
        return False, "honors_retry_after(413) must be True (retryafter.py not updated)"
    if data.get("ra_503") is not True:
        return False, "regression: honors_retry_after(503) must stay True, got %r" % (data.get("ra_503"),)
    if data.get("ra_500") is not False:
        return False, "honors_retry_after(500) must be False, got %r" % (data.get("ra_500"),)

    # end-to-end: 429 + Retry-After header must yield the header value (11.0),
    # which requires policy (retryable) AND retryafter (honored) BOTH fixed.
    nw_ra = _as_float(data.get("nw_429_retryafter"))
    if nw_ra != 11.0:
        return False, ("client.next_wait(429, headers={Retry-After:11}) must return 11.0, "
                       "got %r -- this needs policy.py (retryable) AND retryafter.py "
                       "(honored) BOTH consistent" % (data.get("nw_429_retryafter"),))

    # regressions on previously-correct paths.
    if _as_float(data.get("nw_503_retryafter")) != 7.0:
        return False, "regression: next_wait(503, Retry-After:7) must return 7.0, got %r" % (data.get("nw_503_retryafter"),)
    if _as_float(data.get("nw_500_attempt1")) != 0.5:
        return False, "regression: next_wait(500, attempt=1) must return 0.5, got %r" % (data.get("nw_500_attempt1"),)
    if data.get("nw_404_attempt1") is not None:
        return False, "regression: next_wait(404) must return None, got %r" % (data.get("nw_404_attempt1"),)

    if data.get("errors"):
        return False, "behavioral probe collected errors: %r" % (data.get("errors"),)

    return True, "behavior OK"


def verify(workdir):
    checks = [
        ("distractors_unchanged", _check_distractors_unchanged),
        ("consistency", _check_consistency),
        ("behavior", _check_behavior),
    ]
    for name, fn in checks:
        ok, detail = fn(workdir)
        if not ok:
            return False, "[%s] %s" % (name, detail)
    return True, ("all checks passed: RATE_LIMITED contract is consistent across "
                  "status/policy/backoff/retryafter and drives client.py correctly")
