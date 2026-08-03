"""Blocker downgrade: a repeat NO-VERDICT waives the verify clause. No model/network.

The loom-app incident (2026-08-03): a stateless NO-VERDICT message told the model to 'fix the
CHECK itself' on every attempt, so an environmentally-unrunnable gate (hung typed-lint) looped
for ~19 minutes while the deliverable waited on a human interrupt. Contract under test: the
FIRST no-verdict of a command shape still teaches check-repair (one honest attempt); the SECOND
same-shape no-verdict returns a typed WAIVER — do not retry, deliver the work, record the
limitation verbatim in the deliverable. Real failures and oscillation behavior are unchanged."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sliceagent-core", "src"))

from sliceagent_core.execution import ToolStatus  # noqa: E402
from sliceagent_cli.tools import run_item_verification  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def _no_verdict_runner(cmd):
    # run_item_verification unpacks `ok, output = result` AND reads result.status — mimic OracleResult
    class R(tuple):
        status = ToolStatus.INDETERMINATE
    return R((False, "timed out; process tree was reaped"))


def _red_runner(cmd):
    class R(tuple):
        status = ToolStatus.FAILED
    return R((False, "AssertionError: expected 2 got 3"))


@check
def first_no_verdict_teaches_check_repair():
    attempts: dict = {}
    green, msg = run_item_verification([("w1", ["npx eslint ."])], _no_verdict_runner, attempts)
    assert green == frozenset() and "NO VERDICT" in msg and "Fix the CHECK itself" in msg
    assert "WAIVED" not in msg


@check
def second_same_shape_no_verdict_waives_the_clause():
    attempts: dict = {}
    run_item_verification([("w1", ["npx eslint ."])], _no_verdict_runner, attempts)
    green, msg = run_item_verification([("w1", ["npx eslint src/App.tsx"])], _no_verdict_runner, attempts)
    assert "WAIVED" in msg and "Do NOT run it again" in msg, msg
    assert "record the limitation" in msg and "unrunnable" in msg
    # per-file ladder variants share the two-token shape — the loom spiral's exact move
    assert "`npx eslint`" in msg


@check
def different_shape_gets_its_own_first_chance():
    attempts: dict = {}
    run_item_verification([("w1", ["npx eslint ."])], _no_verdict_runner, attempts)
    _, msg = run_item_verification([("w1", ["npm test"])], _no_verdict_runner, attempts)
    assert "WAIVED" not in msg and "Fix the CHECK itself" in msg


@check
def real_failures_never_waive():
    attempts: dict = {}
    for _ in range(3):
        green, msg = run_item_verification([("w1", ["pytest -q"])], _red_runner, attempts)
    assert "WAIVED" not in msg and "verify failed" in msg
    assert green == frozenset()


@check
def waiver_state_is_per_item():
    attempts: dict = {}
    run_item_verification([("w1", ["npx eslint ."])], _no_verdict_runner, attempts)
    _, msg = run_item_verification([("w2", ["npx eslint ."])], _no_verdict_runner, attempts)
    assert "WAIVED" not in msg, "a different item's clause gets its own honest attempt"


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
