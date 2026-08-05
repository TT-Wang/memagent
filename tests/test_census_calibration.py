"""Census estimator calibration gate.

The byte census is the ONLY per-category cost attribution we have, so its token estimate decides
which region is worth optimizing. It used the repo's RUNTIME budgeting constant
(_TOKENS_PER_BYTE = 2.61 chars/token) while the provider billed 4.26 — every est_tokens ran 1.63x
high. Shares survived (uniform scale) but est_tokens x price, the number an optimization decision
actually reads, was 63% too high. The fix calibrates per run from the run's own provider ledger;
these checks pin the calibration AND the alarm that would have caught the drift on day one.

Run: .venv/bin/python tests/test_census_calibration.py
"""
from __future__ import annotations

import glob
import json
import os

CHECKS = []
_ROOT = os.path.join(os.path.dirname(__file__), "..")
_MIN_REAL_RUN = 10_000        # below this it is the fake-LLM selftest fixture, not a real run


def check(fn):
    CHECKS.append(fn)
    return fn


def _real_censuses():
    for f in glob.glob(os.path.join(_ROOT, "evals", "census_runs", "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(d, dict):
            continue                                   # verdict lists etc.
        tot, meter = d.get("totals_by_category"), d.get("meter") or {}
        if not tot or int(meter.get("in_total") or 0) < _MIN_REAL_RUN:
            continue
        yield os.path.basename(f), d, tot, meter


@check
def stored_census_reconstructs_the_provider_ledger():
    """Every committed census must sum to the provider's own input total."""
    seen = 0
    for name, _d, tot, meter in _real_censuses():
        seen += 1
        est = sum(a["est_tokens"] for a in tot.values())
        billed = int(meter["in_total"])
        drift = abs(est - billed) / billed
        assert drift <= 0.10, (
            f"{name}: categories sum to {est:,} est tokens but the provider billed {billed:,} "
            f"({drift:.0%} off) — dollar attributions are not trustworthy")
    assert seen, "no real census artifact found to validate"


@check
def the_runtime_constant_is_not_a_billing_constant():
    """Guard the ROOT confusion: the runtime budgeting ratio must never be reused as the census
    estimator again. If someone re-imports it, this comparison fails loudly."""
    import sys
    sys.path.insert(0, os.path.join(_ROOT, "packages", "sliceagent-core", "src"))
    from sliceagent_core.execution import _TOKENS_PER_BYTE as runtime_ratio
    for name, d, _tot, _meter in _real_censuses():
        used = float(d.get("tokens_per_byte") or 0)
        assert abs(used - runtime_ratio) > 1e-9, (
            f"{name} used the RUNTIME constant {runtime_ratio} as its billing estimator — "
            "calibrate from meter.in_total / prompt bytes instead")


@check
def the_census_source_calibrates_and_alarms():
    """The producer itself must calibrate per run and mark a >10% mismatch INVALID."""
    src = open(os.path.join(_ROOT, "evals", "serializer_census.py"), encoding="utf-8").read()
    assert "calibrated" in src and "meter_in" in src, "census must calibrate from the run's ledger"
    assert "CENSUS ESTIMATOR DRIFT" in src, "census must alarm on category-vs-ledger drift"


@check
def arm_ledgers_are_provider_reported_not_estimated():
    """The three-arm COMPARISON must never touch an estimator. kimi/mini read provider usage rows
    off their own session wire; sliceagent reads prompt_tokens/completion_tokens from its meter.
    This is why the estimator drift did NOT contaminate any cross-arm claim — pin that."""
    kimi = open(os.path.join(_ROOT, "benchmarks", "kimi_arm.py"), encoding="utf-8").read()
    assert "wire.jsonl" in kimi and "usage" in kimi, "kimi arm must read provider usage records"
    for forbidden in ("_TOKENS_PER_BYTE", "tokens_per_byte"):
        assert forbidden not in kimi, f"kimi arm must not estimate tokens ({forbidden})"
    bench = open(os.path.join(_ROOT, "benchmarks", "run.py"), encoding="utf-8").read()
    assert "prompt_tokens" in bench and "completion_tokens" in bench, \
        "the sliceagent meter must read provider-reported token counts"
    for forbidden in ("_TOKENS_PER_BYTE", "tokens_per_byte"):
        assert forbidden not in bench, f"the bench meter must not estimate tokens ({forbidden})"


def main() -> int:
    failed = 0
    for fn in CHECKS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
