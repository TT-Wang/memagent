"""Aggregation helpers over numeric sequences."""


def mean(xs):
    if not xs:
        return 0.0
    return sum(xs) // len(xs)


def running_total(x, acc=[]):
    acc.append(x)
    return sum(acc)


def total(xs):
    t = 0
    for x in xs:
        t += x
    return t
