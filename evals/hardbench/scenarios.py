"""HARDBENCH — 3 blind, tool-using, multi-round agentic coding scenarios (ColBench successors).

Designed by independent subagents that knew ONLY the ColBench idea (not sliceagent), so they're unbiased.
Each needs real file+shell tools (read/edit/run/iterate) and several human exchanges — far harder than
'ask 2 questions, emit one function'. Pure data here; the driver (run.py) materializes + runs + grades.

Each scenario: files (starting repo), hidden (dropped in at grading), initial (human's 1st message),
human_key (the simulated human's hidden answer-key / system prompt), verify (grader argv), cwd_sub
(run dir relative to repo root, "" = root).
"""

# ── sample access log for LOGROLL (built from rows so tabs are unambiguous) ──────────────────────
_LOG = [
    ["2026-06-19T23:59:01", "Alice", "GET", "/index", "200", "1200"],
    ["2026-06-19T23:59:58", "bob", "GET", "/index", "200", "900"],
    ["2026-06-20T00:00:02", "alice", "GET", "/data", "200", "3400"],
    ["2026-06-20T00:00:02", "alice", "GET", "/data", "200", "3400"],
    ["2026-06-20T00:00:03", "BOB", "POST", "/upload", "201", "50"],
    ["2026-06-20T00:00:05", "carol", "GET", "/index", "404", "0"],
    ["2026-06-20T00:00:06", "carol", "GET", "/index", "404", "0"],
    ["2026-06-20T00:00:06", "carol", "GET", "/index", "404", "0"],
    ["2026-06-20T00:00:09", "dave", "GET", "/data", "500", "0"],
    "MALFORMED LINE WITH NO TABS",
    ["2026-06-20T00:00:11", "Alice", "GET", "/report", "200", "xyz"],
    ["2026-06-20T00:00:12", "bob", "DELETE", "/data", "204", "0"],
    ["2026-06-20T00:00:15", "Eve", "GET", "/index", "200", "1500"],
    ["2026-06-20T00:00:15", "eve", "GET", "/index", "200", "1500"],
    ["2026-06-20T00:00:18", "dave", "GET", "/data", "500", "0"],
    ["2026-06-20T00:00:21", "frank", "GET", "/index", "200", ""],
]
_ACCESS_LOG = "\n".join(r if isinstance(r, str) else "\t".join(r) for r in _LOG) + "\n"


SCENARIOS = {

# ════════════════════════════════════════════════════════════════════════════════════════════════
"ledgerkit-batch": {
    "kind": "integration",
    "verify": ["python3", "-m", "unittest", "discover", "-s", "tests", "-q"],
    "stages": [
        # one increment per turn; each builds on the prior code. The last one forces a re-architecture
        # (validate-all-then-mutate) that contradicts a naive mutate-as-you-go from the earlier stages.
        "We process payments in batches. Please add a function `apply_batch(ledger, postings)` to the "
        "ledgerkit library: it takes the ledger and a list of transfers (each transfer is a "
        "(src_code, dst_code, Money) tuple) and applies them all to the ledger in order, reusing the "
        "library's existing posting machinery. It should return the journal. The existing tests must keep "
        "passing.",
        "Now charge our processing fee on each transfer: 1% of the transfer's amount, paid from that "
        "transfer's source account into an account with code `FEES`. Round the fee to the nearest cent, "
        "and if it's exactly halfway, round up.",
        "Only charge the fee if the ledger actually has a `FEES` account. If there is no `FEES` account, "
        "just apply the transfers with no fee — don't raise an error.",
        "Fee postings themselves must not be charged a fee (that would be circular): any posting whose "
        "destination account code starts with `FEE` is a fee leg and is exempt from the fee.",
        "Two final rules. Reject any transfer whose source and destination are the same account, using the "
        "library's validation error. And make the whole batch atomic: validate every transfer up front, and "
        "if anything is invalid, change nothing at all — no partial application.",
    ],
    "files": {
        "ledgerkit/__init__.py": r'''from .money import Money
from .errors import LedgerError, ValidationError
from .account import Account
from .ledger import Ledger

__all__ = ["Money", "LedgerError", "ValidationError", "Account", "Ledger"]
''',
        "ledgerkit/errors.py": r'''class LedgerError(Exception):
    """Base class for all ledgerkit errors."""


class ValidationError(LedgerError):
    """Raised when an input fails a validation rule.

    Convention: the message is always 'lowercase, no trailing period'
    and names the offending field first, e.g. 'amount must be positive'.
    """
''',
        "ledgerkit/money.py": r'''from .errors import ValidationError


class Money:
    """An integer number of cents. Immutable. Never use floats for money.

    Convention used across ledgerkit: all monetary values are stored as
    int cents. Construct from a major-unit string with Money.parse('12.34').
    """

    __slots__ = ("cents",)

    def __init__(self, cents):
        if not isinstance(cents, int) or isinstance(cents, bool):
            raise ValidationError("cents must be an int")
        self.cents = cents

    @classmethod
    def parse(cls, text):
        text = text.strip()
        neg = text.startswith("-")
        if neg:
            text = text[1:]
        if "." in text:
            whole, frac = text.split(".", 1)
        else:
            whole, frac = text, "0"
        if not whole.isdigit() or not frac.isdigit() or len(frac) > 2:
            raise ValidationError("amount is not a valid money string")
        frac = (frac + "00")[:2]
        cents = int(whole) * 100 + int(frac)
        return cls(-cents if neg else cents)

    def __eq__(self, other):
        return isinstance(other, Money) and other.cents == self.cents

    def __hash__(self):
        return hash(self.cents)

    def __add__(self, other):
        return Money(self.cents + other.cents)

    def __sub__(self, other):
        return Money(self.cents - other.cents)

    def __repr__(self):
        sign = "-" if self.cents < 0 else ""
        c = abs(self.cents)
        return "{}{}.{:02d}".format(sign, c // 100, c % 100)
''',
        "ledgerkit/account.py": r'''from .errors import ValidationError
from .money import Money


class Account:
    """A named balance. Account codes are validated on construction.

    Convention: an account code is 3-8 chars, uppercase letters and digits
    only, and MUST start with a letter. Stored verbatim. Balance starts at 0.
    """

    def __init__(self, code, balance=None):
        if not isinstance(code, str):
            raise ValidationError("code must be a string")
        if not (3 <= len(code) <= 8):
            raise ValidationError("code length must be 3 to 8")
        if not code.isalnum() or not code.isupper() or not code[0].isalpha():
            raise ValidationError("code must be uppercase alnum starting with a letter")
        self.code = code
        self.balance = balance if balance is not None else Money(0)

    def credit(self, amount):
        self.balance = self.balance + amount

    def debit(self, amount):
        self.balance = self.balance - amount

    def __repr__(self):
        return "Account({}, {})".format(self.code, self.balance)
''',
        "ledgerkit/ledger.py": r'''from .errors import ValidationError, LedgerError
from .money import Money
from .account import Account


class Ledger:
    """Holds accounts and applies postings. A posting moves Money from one
    account to another. Postings are recorded in self.journal in order.

    Convention: every mutating op validates first, then mutates. On any
    ValidationError nothing is changed (all-or-nothing).
    """

    def __init__(self):
        self.accounts = {}
        self.journal = []

    def open_account(self, code):
        if code in self.accounts:
            raise ValidationError("account already exists")
        acct = Account(code)
        self.accounts[code] = acct
        return acct

    def get(self, code):
        if code not in self.accounts:
            raise LedgerError("no such account")
        return self.accounts[code]

    def post(self, src, dst, amount):
        if not isinstance(amount, Money):
            raise ValidationError("amount must be Money")
        if amount.cents <= 0:
            raise ValidationError("amount must be positive")
        a = self.get(src)
        b = self.get(dst)
        a.debit(amount)
        b.credit(amount)
        self.journal.append((src, dst, amount.cents))

    def balance(self, code):
        return self.get(code).balance
''',
        "tests/test_ledger.py": r'''import unittest

from ledgerkit import Ledger, Money, ValidationError, LedgerError


class TestLedger(unittest.TestCase):
    def test_open_and_post(self):
        lg = Ledger()
        lg.open_account("CASH")
        lg.open_account("RENT")
        lg.post("CASH", "RENT", Money.parse("100.00"))
        self.assertEqual(lg.balance("CASH"), Money(-10000))
        self.assertEqual(lg.balance("RENT"), Money(10000))

    def test_bad_code(self):
        lg = Ledger()
        with self.assertRaises(ValidationError):
            lg.open_account("xx")

    def test_negative_amount_rejected(self):
        lg = Ledger()
        lg.open_account("CASH")
        lg.open_account("RENT")
        with self.assertRaises(ValidationError):
            lg.post("CASH", "RENT", Money(-1))

    def test_missing_account(self):
        lg = Ledger()
        with self.assertRaises(LedgerError):
            lg.balance("NOPE")


if __name__ == "__main__":
    unittest.main()
''',
    },
    "hidden": {
        "tests/test_batch_hidden.py": r'''import unittest

from ledgerkit import Ledger, Money, ValidationError, LedgerError
try:                                          # the task didn't dictate the module name — accept any
    from ledgerkit.batch import apply_batch   # reasonable placement (batch.py, package top-level, …)
except ImportError:
    from ledgerkit import apply_batch


def fresh(*codes):
    lg = Ledger()
    for c in codes:
        lg.open_account(c)
    return lg


class TestApplyBatch(unittest.TestCase):
    def test_basic_no_fees_account(self):
        lg = fresh("CASH", "RENT")
        apply_batch(lg, [("CASH", "RENT", Money.parse("100.00"))])
        self.assertEqual(lg.balance("CASH"), Money(-10000))
        self.assertEqual(lg.balance("RENT"), Money(10000))
        self.assertEqual(lg.journal, [("CASH", "RENT", 10000)])

    def test_fee_applied_when_fees_exists(self):
        lg = fresh("CASH", "RENT", "FEES")
        apply_batch(lg, [("CASH", "RENT", Money.parse("100.00"))])
        self.assertEqual(lg.balance("CASH"), Money(-10100))
        self.assertEqual(lg.balance("RENT"), Money(10000))
        self.assertEqual(lg.balance("FEES"), Money(100))
        self.assertEqual(lg.journal, [("CASH", "RENT", 10000), ("CASH", "FEES", 100)])

    def test_fee_rounds_half_up(self):
        lg = fresh("CASH", "RENT", "FEES")
        apply_batch(lg, [("CASH", "RENT", Money(150))])
        self.assertEqual(lg.balance("FEES"), Money(2))
        lg2 = fresh("CASH", "RENT", "FEES")
        apply_batch(lg2, [("CASH", "RENT", Money(149))])
        self.assertEqual(lg2.balance("FEES"), Money(1))

    def test_fee_leg_to_fee_account_not_double_charged(self):
        lg = fresh("CASH", "FEEPNL", "FEES")
        apply_batch(lg, [("CASH", "FEEPNL", Money.parse("100.00"))])
        self.assertEqual(lg.balance("FEES"), Money(0))
        self.assertEqual(lg.journal, [("CASH", "FEEPNL", 10000)])

    def test_self_post_rejected(self):
        lg = fresh("CASH", "FEES")
        with self.assertRaises(ValidationError):
            apply_batch(lg, [("CASH", "CASH", Money(100))])

    def test_atomicity_on_failure(self):
        lg = fresh("CASH", "RENT", "FEES")
        with self.assertRaises(ValidationError):
            apply_batch(lg, [("CASH", "RENT", Money.parse("50.00")), ("RENT", "RENT", Money(1))])
        self.assertEqual(lg.balance("CASH"), Money(0))
        self.assertEqual(lg.balance("RENT"), Money(0))
        self.assertEqual(lg.balance("FEES"), Money(0))
        self.assertEqual(lg.journal, [])

    def test_atomicity_missing_account(self):
        lg = fresh("CASH", "FEES")
        with self.assertRaises(LedgerError):
            apply_batch(lg, [("CASH", "GONE", Money(100))])
        self.assertEqual(lg.balance("CASH"), Money(0))
        self.assertEqual(lg.journal, [])

    def test_order_and_multiple(self):
        lg = fresh("CASH", "RENT", "FOOD", "FEES")
        apply_batch(lg, [("CASH", "RENT", Money.parse("100.00")), ("CASH", "FOOD", Money.parse("50.00"))])
        self.assertEqual(lg.journal, [("CASH", "RENT", 10000), ("CASH", "FEES", 100),
                                      ("CASH", "FOOD", 5000), ("CASH", "FEES", 50)])
        self.assertEqual(lg.balance("FEES"), Money(150))


if __name__ == "__main__":
    unittest.main()
''',
    },
},

# ════════════════════════════════════════════════════════════════════════════════════════════════
"ledger-green": {
    "kind": "debugging",
    "verify": ["python3", "-m", "unittest", "discover", "-s", "tests", "-q"],
    "stages": [
        # incremental: fix the obvious failures first, then surface a deeper bug, then a refinement on the
        # SAME code that contradicts a naive fix (a refund/undo) — forcing the agent to revisit its work.
        "The ledger package's test suite is failing — run it and fix whatever's broken so the account tests "
        "pass. Just get them green.",
        "There's also a transfer problem: when a transfer fails because the source has insufficient funds, "
        "the destination account still gets credited. Fix `transfer` so that a failed transfer changes "
        "nothing.",
        "One more requirement on that fix: a failed transfer must leave NO trace at all — not even an entry "
        "in either account's history. A fix that deposits and then reverses it (leaving history entries) is "
        "not acceptable; make sure nothing is ever recorded for a failed transfer.",
    ],
    "files": {
        "money.py": r'''"""Money amounts stored as integer cents."""


def parse_amount(text):
    """Parse a string like '12.50' or '-3.05' into integer cents."""
    text = text.strip()
    sign = 1
    if text.startswith("-"):
        sign = -1
        text = text[1:]
    if "." in text:
        whole, frac = text.split(".")
        frac = (frac + "00")[:2]
    else:
        whole, frac = text, "00"
    return sign * (int(whole) * 100 + int(frac))


def format_amount(cents):
    """Format integer cents as a string like '12.50' or '-3.05'."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return "{}{}.{:02d}".format(sign, cents // 100, cents % 100)
''',
        "account.py": r'''"""A simple append-only account ledger."""

from money import parse_amount, format_amount


class InsufficientFunds(Exception):
    pass


class Account:
    def __init__(self, opening="0.00"):
        self.balance = opening
        self.history = []

    def deposit(self, amount):
        cents = parse_amount(amount)
        self.balance += cents
        self.history.append(("deposit", cents))

    def withdraw(self, amount):
        cents = parse_amount(amount)
        if cents >= self.balance:
            raise InsufficientFunds(format_amount(self.balance))
        self.balance -= cents
        self.history.append(("withdraw", cents))

    def statement(self):
        lines = []
        for kind, cents in self.history:
            lines.append("{}: {}".format(kind, format_amount(cents)))
        lines.append("balance: {}".format(format_amount(self.balance)))
        return "\n".join(lines)
''',
        "transfer.py": r'''"""Transfer money between two accounts atomically."""

from money import parse_amount


def transfer(src, dst, amount):
    """Move `amount` from src to dst. If src lacks funds, neither changes."""
    dst.deposit(amount)
    src.withdraw(amount)
    return parse_amount(amount)
''',
        "tests/test_ledger.py": r'''import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from account import Account, InsufficientFunds
from transfer import transfer


class TestAccount(unittest.TestCase):
    def test_deposit_increases_balance(self):
        a = Account("10.00")
        a.deposit("5.50")
        self.assertEqual(a.balance, 1550)

    def test_withdraw_decreases_balance(self):
        a = Account("10.00")
        a.withdraw("4.00")
        self.assertEqual(a.balance, 600)

    def test_withdraw_exact_balance_allowed(self):
        a = Account("10.00")
        a.withdraw("10.00")
        self.assertEqual(a.balance, 0)

    def test_overdraft_rejected(self):
        a = Account("10.00")
        with self.assertRaises(InsufficientFunds):
            a.withdraw("10.01")
        self.assertEqual(a.balance, 1000)


class TestTransfer(unittest.TestCase):
    def test_transfer_moves_funds(self):
        a = Account("10.00")
        b = Account("0.00")
        transfer(a, b, "3.00")
        self.assertEqual(a.balance, 700)
        self.assertEqual(b.balance, 300)

    def test_failed_transfer_is_atomic(self):
        a = Account("2.00")
        b = Account("0.00")
        with self.assertRaises(InsufficientFunds):
            transfer(a, b, "5.00")
        self.assertEqual(a.balance, 200)
        self.assertEqual(b.balance, 0)


if __name__ == "__main__":
    unittest.main()
''',
    },
    "hidden": {
        "tests/test_heldout.py": r'''import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from account import Account, InsufficientFunds
from transfer import transfer


class TestHeldout(unittest.TestCase):
    def test_failed_transfer_into_empty_dst_is_atomic(self):
        a = Account("2.00")
        b = Account("0.00")
        with self.assertRaises(InsufficientFunds):
            transfer(a, b, "5.00")
        self.assertEqual(a.balance, 200)
        self.assertEqual(b.balance, 0)

    def test_history_not_polluted_on_failed_transfer(self):
        a = Account("2.00")
        b = Account("0.00")
        with self.assertRaises(InsufficientFunds):
            transfer(a, b, "5.00")
        self.assertEqual(b.history, [])
        self.assertEqual(a.history, [])


if __name__ == "__main__":
    unittest.main()
''',
    },
},

# ════════════════════════════════════════════════════════════════════════════════════════════════
"logroll": {
    "kind": "evolving-cli",
    "verify": ["python3", "tests/verify.py"],
    "stages": [
        # one requirement per turn; S4 (CSV-default) and S5 (adjacency dedup) contradict the natural earlier
        # build, forcing the agent to refactor code it wrote in earlier turns.
        "I've got a server access log in sample/access.log and I want a little Python tool, tool.py, that "
        "reads it and prints a summary of who's been hitting the server — each user and how many requests "
        "they made. The log file is the first argument. Run it on the sample so I can see it works.",
        "'Alice' and 'alice' are the same person (also BOB/bob, Eve/eve) — treat usernames case-insensitively "
        "and show them lowercased.",
        "Order the output by most requests first; break ties alphabetically by username.",
        "Change the default output to CSV: the first line must be exactly `user,requests`, then one row per "
        "user in that sorted order. Keep the readable table too, but only when I pass `--pretty`.",
        "The log double-logs sometimes — if the same user hits the same path in the same second, that's really "
        "one request, not several; don't count the repeats. But only collapse them when they're back-to-back "
        "(adjacent lines).",
        "Add a `--errors-only` flag that only counts requests whose status is 400 or above. And add a third "
        "column `bytes` (so the CSV header becomes `user,requests,bytes`) = total bytes served to each user; "
        "a line whose byte count isn't a clean number counts as 0 bytes but still counts as a request.",
    ],
    "files": {
        "tool.py": r'''#!/usr/bin/env python3
"""LOGROLL: summarize a server access log. (skeleton)"""
import sys


def main(argv):
    # TODO: implement
    print("not implemented", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
''',
        "README.md": "# LOGROLL\n\nSummarize a server access log.\n\nUsage (to be defined):\n    python tool.py <logfile> [options]\n",
        "sample/access.log": _ACCESS_LOG,
    },
    "hidden": {
        "tests/verify.py": r'''#!/usr/bin/env python3
"""Hidden deterministic verifier for LOGROLL. Run from repo root."""
import subprocess, sys, os

LOG = os.path.join("sample", "access.log")
PY = sys.executable


def run(args):
    r = subprocess.run([PY, "tool.py"] + args, capture_output=True, text=True)
    return r.stdout, r.stderr, r.returncode


def norm(s):
    lines = [ln.rstrip() for ln in s.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


checks = []

expected1 = ["user,requests,bytes", "alice,3,4600", "bob,3,950", "carol,2,0",
             "dave,2,0", "eve,1,1500", "frank,1,0"]
out, err, rc = run([LOG])
checks.append(("default CSV w/ dedup+bytes+sort", norm(out) == expected1, expected1, norm(out)))

expected2 = ["user,requests,bytes", "carol,2,0", "dave,2,0"]
out, err, rc = run([LOG, "--errors-only"])
checks.append(("--errors-only filter+dedup", norm(out) == expected2, expected2, norm(out)))

out, err, rc = run([LOG, "--pretty"])
pretty = norm(out)
not_csv = (pretty[:1] != ["user,requests,bytes"])
has_rows = (any("alice" in l and "3" in l and "4600" in l for l in pretty)
            and any("frank" in l and "1500" not in l for l in pretty))
checks.append(("--pretty is human table w/ data", not_csv and has_rows and len(pretty) >= 6, "non-CSV table", pretty))

out, err, rc = run([LOG])
dave_ok = any(l == "dave,2,0" for l in norm(out))
checks.append(("adjacent-only dedup (dave kept 2)", dave_ok, "dave,2,0 present", norm(out)))

out, err, rc = run([LOG])
total = 0
for l in norm(out)[1:]:
    parts = l.split(",")
    if len(parts) == 3:
        total += int(parts[1])
checks.append(("malformed skipped (sum of requests == 12)", total == 12, 12, total))

out, err, rc = run([LOG])
no_caps = all(l.split(",")[0].islower() or l == "user,requests,bytes" for l in norm(out) if l)
checks.append(("usernames lowercased", no_caps, "all lowercase", norm(out)))

passed = 0
for name, ok, exp, got in checks:
    print("[{}] {}".format("PASS" if ok else "FAIL", name))
    if not ok:
        print("    expected:", exp)
        print("    got:     ", got)
    passed += ok

print("\n{}/{} checks passed".format(passed, len(checks)))
if passed == len(checks):
    print("ALL 6 CHECKS PASSED")
    sys.exit(0)
sys.exit(1)
''',
    },
},

}
