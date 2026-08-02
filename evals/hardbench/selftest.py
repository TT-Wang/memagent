"""Offline self-test: prove each scenario's grading is sound BEFORE any live run.
Checks (a) the STARTING repo is in the intended state, and (b) the hidden verifier PASSES once the
reference solution is applied. No LLM, no network."""
import os
import sys
import tempfile
import shutil
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hardbench.scenarios import SCENARIOS          # noqa: E402
from hardbench.run import setup_repo, grade        # noqa: E402

REF = {
    "ledgerkit-batch": {"ledgerkit/batch.py": r'''from .errors import ValidationError, LedgerError
from .money import Money


def apply_batch(ledger, postings):
    if not isinstance(postings, list):
        raise ValidationError("postings must be a list")
    for item in postings:
        if not (isinstance(item, tuple) and len(item) == 3):
            raise ValidationError("posting must be a 3-tuple")
        src, dst, amount = item
        if not isinstance(amount, Money):
            raise ValidationError("amount must be Money")
        if amount.cents <= 0:
            raise ValidationError("amount must be positive")
        if src == dst:
            raise ValidationError("src and dst must differ")
        if src not in ledger.accounts or dst not in ledger.accounts:
            raise LedgerError("no such account")
    has_fees = "FEES" in ledger.accounts
    for src, dst, amount in postings:
        ledger.post(src, dst, amount)
        if has_fees and not dst.startswith("FEE"):
            fee_cents = (amount.cents + 50) // 100
            if fee_cents > 0:
                ledger.post(src, "FEES", Money(fee_cents))
    return ledger.journal
'''},
    "ledger-green": {
        "account.py": r'''"""A simple append-only account ledger."""

from money import parse_amount, format_amount


class InsufficientFunds(Exception):
    pass


class Account:
    def __init__(self, opening="0.00"):
        self.balance = parse_amount(opening)
        self.history = []

    def deposit(self, amount):
        cents = parse_amount(amount)
        self.balance += cents
        self.history.append(("deposit", cents))

    def withdraw(self, amount):
        cents = parse_amount(amount)
        if cents > self.balance:
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
    src.withdraw(amount)
    dst.deposit(amount)
    return parse_amount(amount)
''',
    },
    "logroll": {"tool.py": r'''#!/usr/bin/env python3
import sys


def parse(path, errors_only):
    rows = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            parts = line.split("\t")
            if len(parts) != 6:
                continue
            ts, user, method, pathf, status, bytes_s = parts
            if errors_only:
                try:
                    if int(status) < 400:
                        continue
                except ValueError:
                    continue
            user = user.lower()
            try:
                nbytes = int(bytes_s)
            except ValueError:
                nbytes = 0
            rows.append((ts, user, pathf, nbytes))
    return rows


def aggregate(rows):
    counts, bytes_tot = {}, {}
    prev = None
    for ts, user, pathf, nbytes in rows:
        key = (user, pathf, ts)
        if key == prev:
            continue
        prev = key
        counts[user] = counts.get(user, 0) + 1
        bytes_tot[user] = bytes_tot.get(user, 0) + nbytes
    return counts, bytes_tot


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    if not args:
        print("usage: tool.py <logfile> [--pretty] [--errors-only]", file=sys.stderr)
        return 1
    rows = parse(args[0], "--errors-only" in flags)
    counts, bytes_tot = aggregate(rows)
    ordered = sorted(counts, key=lambda u: (-counts[u], u))
    if "--pretty" in flags:
        w = max([len("user")] + [len(u) for u in ordered])
        print("{}  requests  bytes".format("user".ljust(w)))
        for u in ordered:
            print("{}  {:>8}  {}".format(u.ljust(w), counts[u], bytes_tot[u]))
    else:
        print("user,requests,bytes")
        for u in ordered:
            print("{},{},{}".format(u, counts[u], bytes_tot[u]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''},
}


def start_state(sc, root):
    try:
        r = subprocess.run(sc["verify"], cwd=root, capture_output=True, text=True, timeout=60)
        return r.returncode
    except Exception as e:  # noqa: BLE001
        return f"err:{type(e).__name__}"


ok = True
for name, sc in SCENARIOS.items():
    repo = tempfile.mkdtemp(prefix=f"hbtest_{name}_")
    setup_repo(sc, repo)
    rc_start = start_state(sc, repo)            # before fix, before hidden
    for rel, content in REF[name].items():
        p = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(p) or repo, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    passed, out = grade(sc, repo)              # drops hidden + verifies the reference
    status = "OK" if passed else "BROKEN"
    print(f"[{status}] {name:16} start_verify_rc={rc_start!s:10} reference_passes={passed}")
    if not passed:
        ok = False
        print("   verify tail:\n   " + out[-600:].replace("\n", "\n   "))
    shutil.rmtree(repo, ignore_errors=True)

print("\n" + ("ALL SCENARIOS GRADE CORRECTLY" if ok else "SOME SCENARIOS BROKEN"))
sys.exit(0 if ok else 1)
