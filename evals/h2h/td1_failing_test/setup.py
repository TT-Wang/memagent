import os


# ---------------------------------------------------------------------------
# This scenario builds a small, coherent integer-cents expense-splitting library
# (money.py, allocator.py, ledger.py) plus a unittest suite (test_split.py).
#
# The library carries a REAL logic bug in allocator.allocate(): when a total of
# cents does not divide evenly across the weights, the leftover pennies are all
# dumped onto the LAST share instead of being distributed by the largest-
# remainder rule. The sum still reconciles (no penny lost), so the "total is
# preserved" tests stay green -- the defect only shows up as an UNFAIR split.
#
# The test suite ships with ONE failing test (test_leftover_pennies_are_fair)
# that pins the correct largest-remainder behavior, and several passing tests
# (money parsing/formatting/rounding, allocator conservation + validation,
# ledger zero-sum). The task is to make the failing test pass WITHOUT breaking
# the others.
#
# The bug is planted by string replacement of a single, unique line so it is
# deterministic and the reference fix can revert exactly that line.
# ---------------------------------------------------------------------------

# The correct allocator tail (largest-remainder distribution) and the planted
# buggy tail (dump the whole leftover onto the last share). The reference fix
# swaps _BUG back to _GOOD.
_GOOD = """    leftover = total_cents - sum(shares)
    # Hand out the leftover cents one apiece to the shares whose discarded
    # fractional part (remainder) was largest; ties break toward the lower
    # index, which keeps the split deterministic and as fair as cents allow.
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for k in range(leftover):
        shares[order[k]] += 1
    return shares
"""

_BUG = """    leftover = total_cents - sum(shares)
    # Hand out the leftover cents one apiece to the shares whose discarded
    # fractional part (remainder) was largest; ties break toward the lower
    # index, which keeps the split deterministic and as fair as cents allow.
    shares[-1] += leftover
    return shares
"""


_MONEY = '''\
"""money: integer-cents money helpers (no floats in storage).

All amounts are carried internally as whole integer cents to avoid binary
float drift. ``parse`` reads a human string like "12.50" into cents,
``format`` renders cents back to a 2-decimal string, and ``round_half_up``
rounds a fractional cent count to the nearest whole cent with ties going away
from zero -- the convention used for tax and interest lines.
"""
from __future__ import annotations


class MoneyError(ValueError):
    pass


def parse(text):
    """Parse a decimal-money string into integer cents.

    Accepts an optional leading '-', an integer part, and an optional
    fractional part of at most two digits: "12", "12.5", "-3.07".
    """
    t = text.strip()
    if not t:
        raise MoneyError("empty money string")
    neg = t.startswith("-")
    if neg:
        t = t[1:]
    if "." in t:
        whole, frac = t.split(".", 1)
    else:
        whole, frac = t, ""
    if len(frac) > 2:
        raise MoneyError("too many fractional digits: %r" % text)
    if not whole.isdigit() or (frac and not frac.isdigit()):
        raise MoneyError("not a money value: %r" % text)
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    return -cents if neg else cents


def format(cents):
    """Render integer cents as a signed 2-decimal string."""
    neg = cents < 0
    cents = abs(int(cents))
    s = "%d.%02d" % (cents // 100, cents % 100)
    return ("-" + s) if neg else s


def round_half_up(numerator, denominator):
    """Divide ``numerator`` by ``denominator`` and round to the nearest whole
    cent, with ties going AWAY from zero (round-half-up for positives)."""
    if denominator == 0:
        raise MoneyError("division by zero")
    neg = (numerator < 0) ^ (denominator < 0)
    n, d = abs(numerator), abs(denominator)
    q, r = divmod(n, d)
    if r * 2 >= d:
        q += 1
    return -q if neg else q
'''


# allocator.py is assembled from a fixed head plus the (correct) tail, then the
# tail is corrupted in setup() so the planted bug is a single-line replacement
# the reference fix can revert exactly.
_ALLOCATOR_HEAD = '''\
"""allocator: split a whole-cent total across integer weights with NO pennies
lost or invented. The returned shares always sum back to the input total.

When the total does not divide evenly, the leftover cents are distributed by
the largest-remainder rule: each share first gets the floor of its exact
proportion, then the remaining cents are handed out one apiece to the shares
whose discarded fractional part was largest (ties toward the lower index), so
the allocation is as fair and stable as integer cents allow.
"""
from __future__ import annotations


class AllocationError(ValueError):
    pass


def allocate(total_cents, weights):
    """Return a list of integer-cent shares, one per weight, summing to
    ``total_cents``."""
    if not weights:
        raise AllocationError("need at least one weight")
    if any(w < 0 for w in weights):
        raise AllocationError("weights must be non-negative")
    s = sum(weights)
    if s == 0:
        raise AllocationError("weights sum to zero")

    shares = []
    remainders = []
    for i, w in enumerate(weights):
        q, r = divmod(total_cents * w, s)
        shares.append(q)
        remainders.append(r)

'''


_LEDGER = '''\
"""ledger: a tiny shared-expense ledger built on money + allocator.

An ``Expense`` has a payer, an integer-cent amount, and participants with
integer weights (default weight 1 each = even split). ``shares()`` splits the
amount across participants through the allocator (so no penny is lost), and
``Ledger.balances()`` nets every member's paid-vs-owed position across all
recorded expenses.
"""
from __future__ import annotations

from allocator import allocate


class Expense:
    def __init__(self, payer, amount_cents, weights):
        # weights: dict of member -> int weight
        if amount_cents < 0:
            raise ValueError("amount must be non-negative")
        if not weights:
            raise ValueError("need participants")
        self.payer = payer
        self.amount_cents = amount_cents
        self.weights = dict(weights)

    def shares(self):
        """Return member -> owed integer cents for this expense."""
        members = list(self.weights)
        ws = [self.weights[m] for m in members]
        parts = allocate(self.amount_cents, ws)
        return {m: p for m, p in zip(members, parts)}


class Ledger:
    def __init__(self):
        self.expenses = []

    def add(self, expense):
        self.expenses.append(expense)

    def balances(self):
        """member -> net cents (positive = is owed money, negative = owes)."""
        net = {}
        for e in self.expenses:
            net[e.payer] = net.get(e.payer, 0) + e.amount_cents
            for m, owed in e.shares().items():
                net[m] = net.get(m, 0) - owed
        return net
'''


_TEST = '''\
"""Test suite for the expense-splitting library.

Run with:  python -m unittest -v test_split

Most tests pass already. ONE test -- test_leftover_pennies_are_fair in
TestAllocatorFairness -- currently FAILS: it pins the correct largest-remainder
behavior for leftover cents. Make it pass without breaking any of the others.
"""
import unittest

import money
from allocator import allocate, AllocationError
from ledger import Expense, Ledger


class TestMoney(unittest.TestCase):
    def test_parse_basic(self):
        self.assertEqual(money.parse("12.50"), 1250)
        self.assertEqual(money.parse("12"), 1200)
        self.assertEqual(money.parse("0.07"), 7)
        self.assertEqual(money.parse("-3.05"), -305)

    def test_format(self):
        self.assertEqual(money.format(1250), "12.50")
        self.assertEqual(money.format(7), "0.07")
        self.assertEqual(money.format(-305), "-3.05")

    def test_round_half_up(self):
        self.assertEqual(money.round_half_up(5, 2), 3)    # 2.5 -> 3
        self.assertEqual(money.round_half_up(4, 2), 2)
        self.assertEqual(money.round_half_up(-5, 2), -3)  # tie away from zero


class TestAllocatorConserves(unittest.TestCase):
    def test_sum_is_preserved(self):
        # The total must never gain or lose a cent, however it is divided.
        self.assertEqual(sum(allocate(100, [1, 1, 1])), 100)
        self.assertEqual(sum(allocate(101, [1, 1, 1, 1, 1, 1, 1])), 101)
        self.assertEqual(sum(allocate(9999, [3, 2, 1])), 9999)

    def test_even_division(self):
        self.assertEqual(allocate(90, [1, 1, 1]), [30, 30, 30])

    def test_rejects_bad_weights(self):
        with self.assertRaises(AllocationError):
            allocate(100, [])
        with self.assertRaises(AllocationError):
            allocate(100, [0, 0])


class TestLedger(unittest.TestCase):
    def test_balances_zero_sum(self):
        led = Ledger()
        led.add(Expense("alice", 100, {"alice": 1, "bob": 1, "cara": 1}))
        # Every cent paid is owed by someone: the net must sum to zero.
        self.assertEqual(sum(led.balances().values()), 0)


class TestAllocatorFairness(unittest.TestCase):
    def test_leftover_pennies_are_fair(self):
        # 100 cents over three equal shares: each is 33.33..; the one extra
        # cent must go to the FIRST share (largest remainder, ties to the
        # lowest index), NOT be dumped onto the last share.
        self.assertEqual(allocate(100, [1, 1, 1]), [34, 33, 33])
        # 101 cents over seven equal shares: the three leftover cents are
        # spread one-each across the first three shares, never piled on one.
        self.assertEqual(
            allocate(101, [1, 1, 1, 1, 1, 1, 1]),
            [15, 15, 15, 14, 14, 14, 14],
        )


if __name__ == "__main__":
    unittest.main()
'''


def setup(workdir):
    """Build the expense-splitting library + test suite in ``workdir`` and plant
    the leftover-penny allocation bug deterministically."""
    os.makedirs(workdir, exist_ok=True)

    # 1) Assemble the CORRECT allocator, then corrupt exactly the tail line so
    #    leftover cents pile onto the last share instead of being distributed.
    good_allocator = _ALLOCATOR_HEAD + _GOOD
    if good_allocator.count(_GOOD) != 1:
        raise RuntimeError("allocator tail fragment is not unique; bad template")
    buggy_allocator = good_allocator.replace(_GOOD, _BUG, 1)
    if buggy_allocator == good_allocator or _BUG not in buggy_allocator:
        raise RuntimeError("failed to plant allocator bug")

    files = {
        "money.py": _MONEY,
        "allocator.py": buggy_allocator,
        "ledger.py": _LEDGER,
        "test_split.py": _TEST,
    }
    for name, body in files.items():
        with open(os.path.join(workdir, name), "w") as f:
            f.write(body)
