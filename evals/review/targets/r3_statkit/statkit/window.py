"""Sliding-window computations."""


def sliding_mean(xs, size):
    out = []
    for i in range(len(xs) - size):
        chunk = xs[i:i + size]
        out.append(sum(chunk) / size)
    return out


def chunk(xs, size):
    return [xs[i:i + size] for i in range(0, len(xs), size)]
