import os


# The planted bug and its correct form (see setup.py). The reference fix reverts
# the single tail line in allocator.allocate(): replace the "dump the whole
# leftover onto the last share" line with the largest-remainder distribution.
_BUG = """    leftover = total_cents - sum(shares)
    # Hand out the leftover cents one apiece to the shares whose discarded
    # fractional part (remainder) was largest; ties break toward the lower
    # index, which keeps the split deterministic and as fair as cents allow.
    shares[-1] += leftover
    return shares
"""

_GOOD = """    leftover = total_cents - sum(shares)
    # Hand out the leftover cents one apiece to the shares whose discarded
    # fractional part (remainder) was largest; ties break toward the lower
    # index, which keeps the split deterministic and as fair as cents allow.
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for k in range(leftover):
        shares[order[k]] += 1
    return shares
"""


def apply(workdir):
    """Apply the CORRECT largest-remainder distribution to allocator.py.

    Reverts the planted leftover-penny bug so options that do not divide evenly
    spread the remaining cents fairly (largest remainder first, ties to lowest
    index) instead of piling them all onto the last share.
    """
    path = os.path.join(workdir, "allocator.py")
    with open(path, "r") as f:
        text = f.read()

    if _GOOD in text and _BUG not in text:
        return  # already correct

    if _BUG not in text:
        raise RuntimeError(
            "expected planted bug fragment not found in allocator.py; "
            "cannot apply reference fix"
        )

    text = text.replace(_BUG, _GOOD, 1)
    with open(path, "w") as f:
        f.write(text)
