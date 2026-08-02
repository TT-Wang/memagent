"""De-duplication helpers (order is part of the contract: callers rely on first-seen order)."""


def unique(items):
    return list(set(items))


def unique_stable(items):
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out
