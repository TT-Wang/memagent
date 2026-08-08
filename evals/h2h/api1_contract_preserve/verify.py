import os
import sys
import json
import subprocess


# ---------------------------------------------------------------------------
# Independent oracle for the API-contract / standing-requirements scenario.
#
# We do NOT trust the agent's report.py or any test the agent can see. We import
# the (possibly-edited) textmetrics.py in a FRESH subprocess and:
#   (1) introspect inspect.signature() of every public function -> the EXACT
#       contract strings must be unchanged (params, defaults, keyword-only '*'),
#   (2) call all four functions on a battery of UNSEEN inputs and dump the
#       results as JSON,
#   (3) report whether the naive helper still exists and whether __optimized__
#       is set (proof the refactor genuinely happened).
#
# The parent (this file) recomputes the GOLDEN expected results with its OWN
# independent reimplementation of the documented behavior and compares -- so the
# agent cannot pass by hard-coding the prompt's example, hacking the CLI layer,
# or leaving the slow internals in place.
# ---------------------------------------------------------------------------

# The EXACT public signatures that form the standing contract. These are the
# string forms inspect.signature() must still produce after the refactor.
_EXPECTED_SIGS = {
    "word_frequencies": "(text, *, normalize=False, top=None)",
    "cooccurrence": "(tokens, window=2)",
    "ngram_counts": "(tokens, n=2)",
    "similarity": "(a, b, *, metric='jaccard')",
}


# A battery of inputs the agent never saw (none of them is the README example).
# Strings for word_frequencies; token LISTS for the others. Chosen to exercise:
# tie-breaking + normalize + top interaction, forward window edges, n-gram edge
# (len < n and n==1), jaccard/overlap incl. empty sides.
_WF_CASES = [
    ("Alpha beta ALPHA gamma beta alpha delta", {"normalize": False, "top": None}),
    ("Alpha beta ALPHA gamma beta alpha delta", {"normalize": False, "top": 2}),
    ("Alpha beta ALPHA gamma beta alpha delta", {"normalize": True, "top": None}),
    ("Alpha beta ALPHA gamma beta alpha delta", {"normalize": True, "top": 3}),
    ("zz yy zz xx yy zz ww", {"normalize": False, "top": 1}),
    ("one one two two three three", {"normalize": False, "top": 2}),
    ("", {"normalize": True, "top": 5}),
    ("solo", {"normalize": False, "top": None}),
]

_COOC_CASES = [
    (["a", "b", "c", "a", "b"], {"window": 1}),
    (["a", "b", "c", "a", "b"], {"window": 2}),
    (["a", "b", "c", "a", "b"], {"window": 4}),
    (["x", "x", "x"], {"window": 2}),
    (["lone"], {"window": 3}),
    ([], {"window": 1}),
]

_NGRAM_CASES = [
    (["p", "q", "r", "p", "q"], {"n": 1}),
    (["p", "q", "r", "p", "q"], {"n": 2}),
    (["p", "q", "r", "p", "q"], {"n": 3}),
    (["short"], {"n": 2}),
    ([], {"n": 1}),
    (["m", "m", "m", "m"], {"n": 2}),
]

_SIM_CASES = [
    (["a", "b", "c"], ["b", "c", "d"], {"metric": "jaccard"}),
    (["a", "b", "c"], ["b", "c", "d"], {"metric": "overlap"}),
    (["a", "a", "b"], ["b", "b", "c", "d"], {"metric": "jaccard"}),
    (["a", "a", "b"], ["b", "b", "c", "d"], {"metric": "overlap"}),
    ([], [], {"metric": "jaccard"}),
    ([], ["x"], {"metric": "jaccard"}),
    ([], ["x"], {"metric": "overlap"}),
    (["k"], ["k"], {"metric": "overlap"}),
]


# ---------------------------------------------------------------------------
# Independent GOLDEN reimplementation of the documented behavior. Kept separate
# from the agent's code; this is the oracle's source of truth.
# ---------------------------------------------------------------------------
def _gold_word_frequencies(text, *, normalize=False, top=None):
    tokens = text.lower().split()
    if not tokens:
        return {}
    counts = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    total = len(tokens)
    if normalize:
        counts = {k: v / total for k, v in counts.items()}
    if top is not None:
        first_seen = {}
        for idx, t in enumerate(tokens):
            if t not in first_seen:
                first_seen[t] = idx
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0], first_seen[kv[0]]))
        counts = dict(ordered[:top])
    return counts


def _gold_cooccurrence(tokens, window=2):
    tokens = list(tokens)
    pairs = {}
    n = len(tokens)
    for i in range(n):
        left = tokens[i]
        for j in range(i + 1, min(i + 1 + window, n)):
            key = (left, tokens[j])
            pairs[key] = pairs.get(key, 0) + 1
    return pairs


def _gold_ngram_counts(tokens, n=2):
    tokens = list(tokens)
    counts = {}
    if len(tokens) < n:
        return counts
    for i in range(len(tokens) - n + 1):
        gram = tuple(tokens[i:i + n])
        counts[gram] = counts.get(gram, 0) + 1
    return counts


def _gold_similarity(a, b, *, metric="jaccard"):
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    if metric == "jaccard":
        union = len(sa | sb)
        return 0.0 if union == 0 else inter / union
    if metric == "overlap":
        if not sa or not sb:
            return 0.0
        return inter / min(len(sa), len(sb))
    raise ValueError("unknown metric")


# JSON cannot represent tuple keys, so we serialize dict results to a sorted
# list of [key, value] with the key rendered as a stable string. Both the child
# and the parent use the SAME rendering so the comparison is exact.
def _render(value):
    if isinstance(value, dict):
        items = []
        for k, v in value.items():
            items.append([_render_key(k), _render(v)])
        items.sort(key=lambda kv: kv[0])
        return {"__dict__": items}
    if isinstance(value, float):
        # Round to a high fixed precision so float formatting is identical on
        # both sides without being brittle.
        return round(value, 12)
    return value


def _render_key(k):
    if isinstance(k, tuple):
        return "T(" + ",".join(map(repr, k)) + ")"
    return repr(k)


def _gold_payload():
    out = {"wf": [], "cooc": [], "ngram": [], "sim": []}
    for text, kw in _WF_CASES:
        out["wf"].append(_render(_gold_word_frequencies(text, **kw)))
    for toks, kw in _COOC_CASES:
        out["cooc"].append(_render(_gold_cooccurrence(toks, **kw)))
    for toks, kw in _NGRAM_CASES:
        out["ngram"].append(_render(_gold_ngram_counts(toks, **kw)))
    for a, b, kw in _SIM_CASES:
        out["sim"].append(_render(_gold_similarity(a, b, **kw)))
    return out


# ---------------------------------------------------------------------------
# Child program: runs against the AGENT'S textmetrics.py with our fresh inputs.
# ---------------------------------------------------------------------------
_CHILD = r'''
import json, sys, inspect
import textmetrics as tm

WF_CASES = __WF__
COOC_CASES = __COOC__
NGRAM_CASES = __NGRAM__
SIM_CASES = __SIM__

def _render_key(k):
    if isinstance(k, tuple):
        return "T(" + ",".join(map(repr, k)) + ")"
    return repr(k)

def _render(value):
    if isinstance(value, dict):
        items = [[_render_key(k), _render(v)] for k, v in value.items()]
        items.sort(key=lambda kv: kv[0])
        return {"__dict__": items}
    if isinstance(value, float):
        return round(value, 12)
    return value

res = {"sigs": {}, "wf": [], "cooc": [], "ngram": [], "sim": [],
       "errors": {}, "marker": None, "naive_present": None, "fatal": None}

try:
    for name in ("word_frequencies", "cooccurrence", "ngram_counts", "similarity"):
        fn = getattr(tm, name)
        res["sigs"][name] = str(inspect.signature(fn))

    res["marker"] = bool(getattr(tm, "__optimized__", False))
    res["naive_present"] = hasattr(tm, "_naive_pairwise_scan")

    for text, kw in WF_CASES:
        res["wf"].append(_render(tm.word_frequencies(text, **kw)))
    for toks, kw in COOC_CASES:
        res["cooc"].append(_render(tm.cooccurrence(list(toks), **kw)))
    for toks, kw in NGRAM_CASES:
        res["ngram"].append(_render(tm.ngram_counts(list(toks), **kw)))
    for a, b, kw in SIM_CASES:
        res["sim"].append(_render(tm.similarity(list(a), list(b), **kw)))

    # Error-path regression guards: these MUST still raise ValueError.
    def _expect_valueerror(thunk):
        try:
            thunk()
            return "NO_RAISE"
        except ValueError:
            return "ValueError"
        except BaseException as e:
            return "%s:%s" % (type(e).__name__, e)
    res["errors"]["cooc_window0"] = _expect_valueerror(
        lambda: tm.cooccurrence(["a", "b"], window=0))
    res["errors"]["ngram_n0"] = _expect_valueerror(
        lambda: tm.ngram_counts(["a", "b"], n=0))
    res["errors"]["sim_bad_metric"] = _expect_valueerror(
        lambda: tm.similarity(["a"], ["a"], metric="cosine"))

    # Keyword-only enforcement MUST survive: calling normalize/top/metric
    # positionally must raise TypeError (proves the '*' marker is intact at the
    # call level, not just in the printed signature string).
    def _expect_typeerror(thunk):
        try:
            thunk()
            return "NO_RAISE"
        except TypeError:
            return "TypeError"
        except BaseException as e:
            return "%s:%s" % (type(e).__name__, e)
    res["errors"]["wf_positional_kwonly"] = _expect_typeerror(
        lambda: tm.word_frequencies("a b a", True))
    res["errors"]["sim_positional_kwonly"] = _expect_typeerror(
        lambda: tm.similarity(["a"], ["b"], "jaccard"))

except BaseException as e:
    res["fatal"] = "%s:%s" % (type(e).__name__, e)

sys.stdout.write("JSON_START")
sys.stdout.write(json.dumps(res))
'''


def _build_child():
    return (
        _CHILD
        .replace("__WF__", repr(_WF_CASES))
        .replace("__COOC__", repr(_COOC_CASES))
        .replace("__NGRAM__", repr(_NGRAM_CASES))
        .replace("__SIM__", repr(_SIM_CASES))
    )


def verify(workdir):
    lib = os.path.join(workdir, "textmetrics.py")
    if not os.path.isfile(lib):
        return False, "textmetrics.py not found in workdir"

    # Drop any cached bytecode so we always test the CURRENT source (an edit in
    # the same wall-clock second can otherwise leave a stale .pyc import reuses).
    pycache = os.path.join(workdir, "__pycache__")
    if os.path.isdir(pycache):
        for fn in os.listdir(pycache):
            if fn.startswith("textmetrics.") and fn.endswith(".pyc"):
                try:
                    os.remove(os.path.join(pycache, fn))
                except OSError:
                    pass

    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-c", _build_child()],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        return False, "could not launch child: %s" % (e,)

    if proc.returncode != 0:
        return False, "child crashed (rc=%d): %s" % (
            proc.returncode, (proc.stderr or proc.stdout)[-600:])
    if "JSON_START" not in proc.stdout:
        return False, "child produced no JSON: %s" % ((proc.stderr or proc.stdout)[-600:])
    try:
        res = json.loads(proc.stdout.split("JSON_START", 1)[1])
    except Exception as e:  # noqa: BLE001
        return False, "could not parse child output: %s :: %r" % (e, proc.stdout[-400:])

    if res.get("fatal"):
        return False, "library raised during exercise: %s" % (res["fatal"],)

    # (1) STANDING CONTRACT: the four public signatures must be byte-identical.
    sigs = res.get("sigs", {})
    for name, expected in _EXPECTED_SIGS.items():
        got = sigs.get(name)
        if got is None:
            return False, "public function %r is missing (contract broken)" % (name,)
        if got != expected:
            return False, ("signature of %r changed: got %r, contract requires %r "
                           "(the public API is a frozen standing requirement)"
                           % (name, got, expected))

    # (2) PROOF OF REFACTOR: naive helper removed AND marker set.
    if res.get("naive_present"):
        return False, ("the quadratic internal helper `_naive_pairwise_scan` is still "
                       "present; the task requires removing it as proof of the refactor")
    if not res.get("marker"):
        return False, "module-level __optimized__ is not True (refactor marker not set)"

    # (3) BEHAVIOR PRESERVED on every unseen input (vs the independent golden).
    gold = _gold_payload()
    for key, label in (("wf", "word_frequencies"), ("cooc", "cooccurrence"),
                       ("ngram", "ngram_counts"), ("sim", "similarity")):
        got_list = res.get(key, [])
        want_list = gold[key]
        if len(got_list) != len(want_list):
            return False, ("%s returned %d results, expected %d (case battery mismatch)"
                           % (label, len(got_list), len(want_list)))
        for idx, (got, want) in enumerate(zip(got_list, want_list)):
            if got != want:
                return False, ("%s case #%d behavior changed: got %r, expected %r "
                               "(behavior must stay identical for every input)"
                               % (label, idx, got, want))

    # (4) REGRESSION GUARDS: error paths and keyword-only enforcement intact.
    errors = res.get("errors", {})
    expect_value = {
        "cooc_window0": "cooccurrence(window=0) must raise ValueError",
        "ngram_n0": "ngram_counts(n=0) must raise ValueError",
        "sim_bad_metric": "similarity(metric='cosine') must raise ValueError",
    }
    for tag, msg in expect_value.items():
        if errors.get(tag) != "ValueError":
            return False, "%s; got %r" % (msg, errors.get(tag))
    expect_type = {
        "wf_positional_kwonly": "word_frequencies' keyword-only marker must reject a "
                                "positional normalize",
        "sim_positional_kwonly": "similarity's keyword-only marker must reject a "
                                 "positional metric",
    }
    for tag, msg in expect_type.items():
        if errors.get(tag) != "TypeError":
            return False, "%s; got %r" % (msg, errors.get(tag))

    return True, ("all checks passed: four public signatures unchanged, behavior "
                  "identical on %d unseen cases, error/keyword-only regressions intact, "
                  "naive helper removed and __optimized__ set"
                  % (len(_WF_CASES) + len(_COOC_CASES) + len(_NGRAM_CASES) + len(_SIM_CASES)))
