"""Paired bootstrap statistics for the prompt A/B suite.

Every metric is measured on the SAME items (review targets / convo cases / task scenarios) for the control
and each variant, so we compare PAIRED differences — per-item difficulty cancels and we isolate the prompt
effect. The harness is noisy (single-run recall swings ~0.1), so we report bootstrap CIs and call a delta
'significant' only when its CI excludes 0. Pure stdlib; seeded for reproducibility.
"""
from __future__ import annotations

import random


def mean(xs) -> float:
    xs = [float(x) for x in xs]
    return sum(xs) / len(xs) if xs else 0.0


def bootstrap_ci(values, iters: int = 5000, alpha: float = 0.05, seed: int = 12345):
    """Bootstrap CI of the mean of `values`. Returns (mean, lo, hi)."""
    values = [float(v) for v in values]
    if not values:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    lo = means[int((alpha / 2) * iters)]
    hi = means[min(int((1 - alpha / 2) * iters), iters - 1)]
    return (mean(values), lo, hi)


def paired_diff(control, variant, iters: int = 5000, alpha: float = 0.05, seed: int = 12345):
    """control, variant: equal-length per-item score lists (SAME item order). Returns the mean paired
    diff (variant - control), a bootstrap CI of that diff, and significant = CI excludes 0."""
    control = [float(v) for v in control]
    variant = [float(v) for v in variant]
    n = min(len(control), len(variant))
    if n == 0:
        return {"diff": 0.0, "lo": 0.0, "hi": 0.0, "significant": False, "n": 0}
    diffs = [variant[i] - control[i] for i in range(n)]
    rng = random.Random(seed)
    boots = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    lo = boots[int((alpha / 2) * iters)]
    hi = boots[min(int((1 - alpha / 2) * iters), iters - 1)]
    return {"diff": mean(diffs), "lo": lo, "hi": hi, "significant": (lo > 0 or hi < 0), "n": n}
