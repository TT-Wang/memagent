import os

# Seed project: auditlog, a tiny audited account service.
# This is the starting repo BEFORE turn 1. It is small and working for the
# features it has (open/deposit/withdraw/balance over an append-only audit
# log). The 9 user turns extend the service. The three package invariants
# (C1 audited, C2 append-only, C3 monotonic timestamps) are stated in the
# log module's docstring and govern every later change.

SEED_LOG = '''\
"""Append-only audit log with a monotonic clock.

Design notes (READ THIS before changing behavior):
  * ``Clock.tick()`` returns 1, 2, 3, ... -- a strictly increasing integer
    timestamp source.
  * ``AuditLog.append(record)`` copies the record, stamps ``seq`` (its index)
    and ``ts`` (the next clock tick), stores it, and returns a copy.
  * ``records()`` returns copies, so callers can never mutate stored history.

Package invariants -- EVERY future change to this package must preserve them:
  C1  AUDITED: each public mutation on the service appends exactly one audit
      record for every account it creates or changes -- never zero, and never
      a single rolled-up summary covering several accounts. Read-only queries
      append nothing.
  C2  APPEND-ONLY: an appended record is immortal. Nothing may rewrite,
      reorder, or delete an existing record; corrections, reversals and
      clean-ups are expressed as NEW records appended on top.
  C3  MONOTONIC: ``ts`` values are strictly increasing in append order.
"""


class Clock:
    """Monotonic integer timestamp source."""

    def __init__(self):
        self._now = 0

    def tick(self):
        self._now += 1
        return self._now


class AuditLog:
    def __init__(self, clock=None):
        self._clock = clock if clock is not None else Clock()
        self._records = []

    def append(self, record):
        rec = dict(record)
        rec["seq"] = len(self._records)
        rec["ts"] = self._clock.tick()
        self._records.append(rec)
        return dict(rec)

    def records(self):
        return [dict(r) for r in self._records]

    def __len__(self):
        return len(self._records)
'''

SEED_SERVICE = '''\
"""Account service whose every mutation is audited.

Design notes (READ THIS before changing behavior):
  * ``_balances`` maps account name -> integer balance. Only OPEN accounts
    live in this dict.
  * The audit log is exposed as the public attribute ``log`` (an
    ``auditlog.log.AuditLog``); read history with ``service.log.records()``.
  * Record shape: {"action": <method name>, "account": <name>, plus any
    action-specific fields such as "amount"}. ``AuditLog.append`` stamps
    ``ts`` and ``seq`` on its own.
  * Error convention: unknown account names raise KeyError; every other
    invalid input or disallowed operation raises ValueError.
  * The three package invariants C1/C2/C3 documented in auditlog/log.py
    govern every method in this class, present and future.
"""

from .log import AuditLog


class AccountService:
    def __init__(self, log=None):
        self.log = log if log is not None else AuditLog()
        self._balances = {}

    # ----- helpers ---------------------------------------------------------
    def _require_account(self, name):
        if name not in self._balances:
            raise KeyError("no such account: %r" % (name,))

    @staticmethod
    def _check_amount(amount):
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("amount must be a positive integer")

    # ----- mutations (each appends exactly one audit record: C1) -----------
    def open_account(self, name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("account name must be a non-empty string")
        if name in self._balances:
            raise ValueError("account already exists: %r" % (name,))
        self._balances[name] = 0
        self.log.append({"action": "open_account", "account": name})

    def deposit(self, name, amount):
        self._require_account(name)
        self._check_amount(amount)
        self._balances[name] += amount
        self.log.append({"action": "deposit", "account": name, "amount": amount})

    def withdraw(self, name, amount):
        self._require_account(name)
        self._check_amount(amount)
        if self._balances[name] < amount:
            raise ValueError("insufficient funds")
        self._balances[name] -= amount
        self.log.append({"action": "withdraw", "account": name, "amount": amount})

    # ----- read-only queries (append nothing: C1) ---------------------------
    def balance(self, name):
        self._require_account(name)
        return self._balances[name]
'''

SEED_README = '''\
# auditlog

A small audited account service used as a teaching toy.

Current capabilities:
  * `open_account` / `deposit` / `withdraw` / `balance`
  * an append-only `AuditLog` with a monotonic clock

See `auditlog/log.py` for the three package invariants (C1 audited, C2
append-only, C3 monotonic timestamps) and `auditlog/service.py` for the
record shape and error conventions. New features must preserve all three
invariants.
'''

SEED_TEST = '''\
"""Smoke tests that ship with the seed (open/deposit/withdraw/balance only).
Keep them green."""
from auditlog.service import AccountService


def test_open_deposit_withdraw():
    svc = AccountService()
    svc.open_account("alice")
    svc.deposit("alice", 100)
    svc.withdraw("alice", 30)
    assert svc.balance("alice") == 70


def test_one_record_per_mutation():
    svc = AccountService()
    svc.open_account("a")
    svc.deposit("a", 5)
    svc.withdraw("a", 2)
    recs = svc.log.records()
    assert [r["action"] for r in recs] == ["open_account", "deposit", "withdraw"]
    ts = [r["ts"] for r in recs]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)


def test_errors():
    svc = AccountService()
    svc.open_account("a")
    try:
        svc.deposit("ghost", 1)
        assert False, "expected KeyError"
    except KeyError:
        pass
    try:
        svc.withdraw("a", 1)
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        svc.open_account("a")
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert len(svc.log.records()) == 1


def test_records_are_copies():
    svc = AccountService()
    svc.open_account("a")
    recs = svc.log.records()
    recs[0]["action"] = "HACK"
    assert svc.log.records()[0]["action"] == "open_account"


if __name__ == "__main__":
    test_open_deposit_withdraw()
    test_one_record_per_mutation()
    test_errors()
    test_records_are_copies()
    print("seed tests ok")
'''


def setup(workdir):
    pkg = os.path.join(workdir, "auditlog")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write("from .log import AuditLog, Clock\nfrom .service import AccountService\n")
    with open(os.path.join(pkg, "log.py"), "w") as f:
        f.write(SEED_LOG)
    with open(os.path.join(pkg, "service.py"), "w") as f:
        f.write(SEED_SERVICE)
    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write(SEED_README)
    tests = os.path.join(workdir, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(tests, "test_seed.py"), "w") as f:
        f.write(SEED_TEST)
