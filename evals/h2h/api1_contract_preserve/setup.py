import os


# ---------------------------------------------------------------------------
# The seed library. Every public function is CORRECT but deliberately NAIVE:
# each one routes through a single quadratic internal helper
# `_naive_pairwise_scan`, which does repeated full linear scans / list.count
# over the entire input (O(n^2)). The task is to optimize the internals to
# linear accumulation while preserving the EXACT public signatures and the
# observable behavior, removing the naive helper and flipping __optimized__.
# ---------------------------------------------------------------------------
_LIB = '''\
"""textmetrics: a tiny, self-contained text-analysis library.

PUBLIC API (these signatures and behaviors are a STANDING CONTRACT -- callers
in the wild depend on them and they MUST NOT change):

    word_frequencies(text, *, normalize=False, top=None) -> dict[str, float]
        Tokenize `text` on whitespace (case-folded) and count each token.
        If `normalize` is True, values are token_count / total_tokens (floats),
        otherwise raw integer counts. If `top` is an int N, only the N
        most-frequent tokens are kept (ties broken by token sort order, then
        by first appearance). `top=None` keeps everything. Empty text -> {}.

    cooccurrence(tokens, window=2) -> dict[tuple[str, str], int]
        For each token, count how often each OTHER token appears within
        `window` positions to its RIGHT (a directed, forward sliding window).
        Keys are (left, right) ordered pairs; value is the number of times
        `right` occurred within `window` positions after an occurrence of
        `left`. `window` must be >= 1 (else ValueError).

    ngram_counts(tokens, n=2) -> dict[tuple[str, ...], int]
        Count contiguous n-grams (as tuples of length `n`) over `tokens`.
        `n` must be >= 1 (else ValueError). If len(tokens) < n -> {}.

    similarity(a, b, *, metric="jaccard") -> float
        Similarity between two token sequences. metric="jaccard" returns
        |set(a) & set(b)| / |set(a) | set(b)| (0.0 if both empty);
        metric="overlap" returns |set(a) & set(b)| / min(|set(a)|,|set(b)|)
        (0.0 if either side is empty). Unknown metric -> ValueError.

IMPLEMENTATION NOTE: the four public functions are currently CORRECT but
NAIVE -- each routes through `_naive_pairwise_scan`, an O(n^2) helper that
re-scans the whole input for every position. This is the part to optimize.
"""

__optimized__ = False


def _naive_pairwise_scan(items):
    """Internal O(n^2) primitive: for each index i, return (item_i, count_i)
    where count_i is the number of occurrences of item_i across the WHOLE
    list, recomputed from scratch every time via list.count.

    This is intentionally quadratic and is the shared slow path under all four
    public functions. The optimization should make the public functions linear
    and REMOVE this helper.
    """
    out = []
    for i in range(len(items)):
        # O(n) recount on every iteration -> O(n^2) overall.
        out.append((items[i], items.count(items[i])))
    return out


def _tokenize(text):
    """Whitespace tokenizer, case-folded. (Cheap; not the bottleneck.)"""
    return text.lower().split()


def word_frequencies(text, *, normalize=False, top=None):
    tokens = _tokenize(text)
    if not tokens:
        return {}
    # Naive: dedupe-by-rescan via the quadratic helper, then build counts.
    scanned = _naive_pairwise_scan(tokens)
    counts = {}
    for tok, cnt in scanned:
        counts[tok] = cnt  # cnt is the global count for tok
    total = len(tokens)
    if normalize:
        counts = {k: v / total for k, v in counts.items()}
    if top is not None:
        # Keep the `top` most frequent; ties broken by token sort order, then
        # first appearance (dict preserves insertion = first appearance).
        first_seen = {}
        for idx, t in enumerate(tokens):
            if t not in first_seen:
                first_seen[t] = idx
        ordered = sorted(
            counts.items(),
            key=lambda kv: (-kv[1], kv[0], first_seen[kv[0]]),
        )
        counts = dict(ordered[:top])
    return counts


def cooccurrence(tokens, window=2):
    if window < 1:
        raise ValueError("window must be >= 1")
    tokens = list(tokens)
    # Touch the naive helper so the slow path is genuinely shared (and so a
    # blind "delete the helper" without rewriting bodies breaks the function).
    _ = _naive_pairwise_scan(tokens)
    pairs = {}
    n = len(tokens)
    for i in range(n):
        left = tokens[i]
        for j in range(i + 1, min(i + 1 + window, n)):
            right = tokens[j]
            key = (left, right)
            pairs[key] = pairs.get(key, 0) + 1
    return pairs


def ngram_counts(tokens, n=2):
    if n < 1:
        raise ValueError("n must be >= 1")
    tokens = list(tokens)
    _ = _naive_pairwise_scan(tokens)
    counts = {}
    if len(tokens) < n:
        return counts
    for i in range(len(tokens) - n + 1):
        gram = tuple(tokens[i:i + n])
        counts[gram] = counts.get(gram, 0) + 1
    return counts


def similarity(a, b, *, metric="jaccard"):
    a = list(a)
    b = list(b)
    _ = _naive_pairwise_scan(a)
    _ = _naive_pairwise_scan(b)
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    if metric == "jaccard":
        union = len(sa | sb)
        if union == 0:
            return 0.0
        return inter / union
    if metric == "overlap":
        if not sa or not sb:
            return 0.0
        return inter / min(len(sa), len(sb))
    raise ValueError("unknown metric: %r" % (metric,))
'''


_CLI = '''\
"""report.py: a tiny CLI built on textmetrics. Not the place to fix anything --
it only demonstrates the public API. Do NOT special-case logic here.
"""
import sys
import textmetrics


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    text = " ".join(argv) if argv else "the cat sat on the mat the cat ran"
    tokens = text.lower().split()
    print("freqs:", textmetrics.word_frequencies(text, top=3))
    print("bigrams:", textmetrics.ngram_counts(tokens, n=2))
    print("cooc(w=2):", textmetrics.cooccurrence(tokens, window=2))
    print("self-sim:", textmetrics.similarity(tokens, tokens))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


_README = '''\
textmetrics
===========

A small, dependency-free text-analysis library plus a `report.py` CLI demo.

Public API (a STANDING CONTRACT -- external callers depend on these EXACT
signatures and behaviors; they must not change):

    word_frequencies(text, *, normalize=False, top=None) -> dict
    cooccurrence(tokens, window=2) -> dict
    ngram_counts(tokens, n=2) -> dict
    similarity(a, b, *, metric="jaccard") -> float

See the module docstring in `textmetrics.py` for the precise behavior of each.

Performance
-----------
The library works correctly but is SLOW: every public function currently runs
through one shared quadratic (O(n^2)) internal pass, so it does not scale to
large documents. A profiling run on a ~200k-token document spends essentially
all of its time in that quadratic pass.

We want the internals optimized to linear time WITHOUT changing the public API
or any observable result.
'''


def setup(workdir):
    """Write the naive-but-correct textmetrics library, a CLI demo, and a
    README describing the performance problem WITHOUT naming the internal
    helper the agent should discover and remove.
    """
    with open(os.path.join(workdir, "textmetrics.py"), "w") as f:
        f.write(_LIB)
    with open(os.path.join(workdir, "report.py"), "w") as f:
        f.write(_CLI)
    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write(_README)
