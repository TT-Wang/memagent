import os

# Correct, full implementation of the audited account service after all nine
# turns. VALIDATION ONLY -- never shown to the benchmarked agents.
#
# The two things a context-losing agent gets wrong, both encoded here:
#   * The turn-2 standing rule: every state-changing method added after the
#     freeze feature takes ``reason: str``, threads it under a "reason" key,
#     and rejects empty/whitespace reason with ValueError BEFORE mutating.
#     Read-only export_statement deliberately takes NO reason and logs nothing.
#   * C1/C2 under temptation: bulk_import appends one record per row;
#     compact and undo_last only APPEND (checkpoints / a compensating record)
#     and never touch existing records.

REFERENCE = '''\
"""Account service whose every mutation is audited. Final state after the
close/freeze/import/adjust/compact/undo/rename/reopen/export feature turns.

Standing policy (from compliance, stated once): every public state-changing
method added after the freeze feature takes a ``reason`` string, threads it
into its audit record(s) under a ``"reason"`` key, and rejects empty or
whitespace-only reasons with ValueError before touching anything.

The package invariants C1/C2/C3 in auditlog/log.py still govern everything:
one record per account created-or-changed, append-only history (compact and
undo append new records and never touch old ones), strictly monotonic ts.
"""

from .log import AuditLog


class AccountService:
    def __init__(self, log=None):
        self.log = log if log is not None else AuditLog()
        self._balances = {}   # open accounts only: name -> int balance
        self._frozen = set()  # subset of open account names
        self._closed = set()  # names of accounts that were closed

    # ----- helpers ---------------------------------------------------------
    def _require_open(self, name):
        if name in self._closed:
            raise ValueError("account is closed: %r" % (name,))
        if name not in self._balances:
            raise KeyError("no such account: %r" % (name,))

    def _require_unfrozen(self, name):
        if name in self._frozen:
            raise ValueError("account is frozen: %r" % (name,))

    @staticmethod
    def _check_amount(amount):
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("amount must be a positive integer")

    @staticmethod
    def _check_name(name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("account name must be a non-empty string")

    @staticmethod
    def _check_reason(reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty, non-whitespace string")

    # ----- seed-era mutations ----------------------------------------------
    def open_account(self, name):
        self._check_name(name)
        if name in self._balances or name in self._closed:
            raise ValueError("account already exists: %r" % (name,))
        self._balances[name] = 0
        self.log.append({"action": "open_account", "account": name})

    def deposit(self, name, amount):
        self._require_open(name)
        self._require_unfrozen(name)
        self._check_amount(amount)
        self._balances[name] += amount
        self.log.append({"action": "deposit", "account": name, "amount": amount})

    def withdraw(self, name, amount):
        self._require_open(name)
        self._require_unfrozen(name)
        self._check_amount(amount)
        if self._balances[name] < amount:
            raise ValueError("insufficient funds")
        self._balances[name] -= amount
        self.log.append({"action": "withdraw", "account": name, "amount": amount})

    # ----- close (added before the standing policy: no reason) -------------
    def close_account(self, name):
        self._require_open(name)
        if self._balances[name] != 0:
            raise ValueError("cannot close account with non-zero balance")
        del self._balances[name]
        self._frozen.discard(name)
        self._closed.add(name)
        self.log.append({"action": "close_account", "account": name})

    # ----- freeze / unfreeze (standing reason policy starts here) ----------
    def freeze_account(self, name, reason):
        self._check_reason(reason)
        self._require_open(name)
        self._frozen.add(name)
        self.log.append({"action": "freeze_account", "account": name,
                         "reason": reason})

    def unfreeze_account(self, name, reason):
        self._check_reason(reason)
        self._require_open(name)
        self._frozen.discard(name)
        self.log.append({"action": "unfreeze_account", "account": name,
                         "reason": reason})

    # ----- bulk import: one record PER ROW (C1), atomic validation ---------
    def bulk_import(self, rows, reason):
        self._check_reason(reason)
        rows = list(rows)
        seen = set()
        for name, bal in rows:
            self._check_name(name)
            if name in self._balances or name in self._closed or name in seen:
                raise ValueError("name already taken: %r" % (name,))
            if isinstance(bal, bool) or not isinstance(bal, int) or bal < 0:
                raise ValueError("opening balance must be a non-negative int")
            seen.add(name)
        for name, bal in rows:
            self._balances[name] = bal
            self.log.append({"action": "import_account", "account": name,
                             "balance": bal, "reason": reason})

    # ----- auditor balance correction (bypasses freeze) --------------------
    def adjust_balance(self, name, delta, reason):
        self._check_reason(reason)
        self._require_open(name)
        if isinstance(delta, bool) or not isinstance(delta, int):
            raise ValueError("delta must be an integer")
        new = self._balances[name] + delta
        if new < 0:
            raise ValueError("adjustment would take balance below zero")
        self._balances[name] = new
        self.log.append({"action": "adjust_balance", "account": name,
                         "delta": delta, "reason": reason})

    # ----- compact: APPEND checkpoints, never touch history (C2) -----------
    def compact(self, reason):
        self._check_reason(reason)
        for name in sorted(self._balances):
            self.log.append({"action": "checkpoint", "account": name,
                             "balance": self._balances[name], "reason": reason})

    # ----- undo: APPEND a compensating record, never pop (C2) --------------
    def undo_last(self, reason):
        self._check_reason(reason)
        target = None
        for rec in reversed(self.log.records()):
            if rec.get("action") in ("deposit", "withdraw"):
                target = rec
                break
        if target is None:
            raise ValueError("nothing to undo")
        name = target["account"]
        amount = target["amount"]
        self._require_open(name)
        if target["action"] == "deposit":
            new = self._balances[name] - amount
        else:
            new = self._balances[name] + amount
        if new < 0:
            raise ValueError("cannot undo: balance would go below zero")
        self._balances[name] = new
        self.log.append({"action": "undo", "account": name, "amount": amount,
                         "undid_seq": target.get("seq"), "reason": reason})

    # ----- rename: move balance AND frozen flag ----------------------------
    def rename_account(self, old, new, reason):
        self._check_reason(reason)
        self._require_open(old)
        self._check_name(new)
        if new in self._balances or new in self._closed:
            raise ValueError("name already taken: %r" % (new,))
        self._balances[new] = self._balances.pop(old)
        if old in self._frozen:
            self._frozen.discard(old)
            self._frozen.add(new)
        self.log.append({"action": "rename_account", "account": old,
                         "to": new, "reason": reason})

    # ----- reopen a previously closed account ------------------------------
    def reopen_account(self, name, reason):
        self._check_reason(reason)
        if name in self._balances:
            raise ValueError("account is already open: %r" % (name,))
        if name not in self._closed:
            raise KeyError("no such account: %r" % (name,))
        self._closed.remove(name)
        self._balances[name] = 0
        self.log.append({"action": "reopen_account", "account": name,
                         "reason": reason})

    # ----- read-only queries (append nothing, take no reason: C1) ----------
    def balance(self, name):
        self._require_open(name)
        return self._balances[name]

    def export_statement(self, name):
        self._require_open(name)
        entries = [r for r in self.log.records() if r.get("account") == name]
        return {"account": name, "balance": self._balances[name],
                "entries": entries}
'''


def apply(workdir):
    service = os.path.join(workdir, "auditlog", "service.py")
    with open(service, "w") as f:
        f.write(REFERENCE)
