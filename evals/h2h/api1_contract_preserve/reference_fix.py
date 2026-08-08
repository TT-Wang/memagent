"""Reference solution (VALIDATION ONLY -- never shown to benchmarked agents).

Optimizes the textmetrics internals to linear accumulation while keeping the
four public signatures and all observable behavior EXACTLY as seeded:
  - removes the quadratic `_naive_pairwise_scan` helper entirely,
  - rewrites each public body to use dict/Counter accumulation,
  - sets the module-level marker `__optimized__ = True`.
The public signatures (incl. keyword-only markers and defaults) are unchanged.
"""
import os


_OPTIMIZED_LIB = '''\
"""textmetrics: a tiny, self-contained text-analysis library.

PUBLIC API (these signatures and behaviors are a STANDING CONTRACT -- callers
in the wild depend on them and they MUST NOT change):

    word_frequencies(text, *, normalize=False, top=None) -> dict[str, float]
    cooccurrence(tokens, window=2) -> dict[tuple[str, str], int]
    ngram_counts(tokens, n=2) -> dict[tuple[str, ...], int]
    similarity(a, b, *, metric="jaccard") -> float

Behavior is documented per-function below; see the README for the contract.
This version is OPTIMIZED: the naive quadratic helper has been removed and each
public function now accumulates in linear time.
"""
from collections import Counter

__optimized__ = True


def _tokenize(text):
    """Whitespace tokenizer, case-folded."""
    return text.lower().split()


def word_frequencies(text, *, normalize=False, top=None):
    tokens = _tokenize(text)
    if not tokens:
        return {}
    # Linear count; preserve first-appearance insertion order.
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
    pairs = {}
    n = len(tokens)
    for i in range(n):
        left = tokens[i]
        upper = i + 1 + window
        if upper > n:
            upper = n
        for j in range(i + 1, upper):
            key = (left, tokens[j])
            pairs[key] = pairs.get(key, 0) + 1
    return pairs


def ngram_counts(tokens, n=2):
    if n < 1:
        raise ValueError("n must be >= 1")
    tokens = list(tokens)
    counts = {}
    if len(tokens) < n:
        return counts
    for i in range(len(tokens) - n + 1):
        gram = tuple(tokens[i:i + n])
        counts[gram] = counts.get(gram, 0) + 1
    return counts


def similarity(a, b, *, metric="jaccard"):
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


# Counter is imported to advertise the linear-accumulation intent even though
# the bodies above use plain dicts; keep it referenced to avoid a lint nit.
_ = Counter
'''


def apply(workdir):
    """Replace textmetrics.py with the optimized-but-equivalent version."""
    path = os.path.join(workdir, "textmetrics.py")
    if not os.path.isfile(path):
        raise RuntimeError("textmetrics.py not found; cannot apply reference fix")
    with open(path, "w") as f:
        f.write(_OPTIMIZED_LIB)
