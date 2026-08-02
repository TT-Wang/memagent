"""One clock knob for every timing-sensitive test in the suite.

Timing pins assert a PAIR of facts — a bound fires while the thing it races has NOT yet happened —
so what they actually pin is the RATIO between two durations, never the absolute milliseconds.
Calibrated on an idle laptop those durations get as tight as 10ms with a 4x margin, and a shared CI
runner's scheduling jitter closes that gap: the pin goes red for a reason that has nothing to do
with the invariant under test.

``SLICE_TEST_TIME_SCALE`` stretches every duration by the SAME factor, so the ratios survive by
construction. Local runs stay at 1.0 (fast); CI sets it higher.

Two rules, both learned the hard way — each cost a red CI round:

1. **Partial scaling is worse than none.** Scaling a budget but not the wait it races retunes the
   INVARIANT instead of the clock. The first attempt scaled the budgets only and broke 7 tests.
2. **The system under test has a clock too.** Scaling only the test's own numbers leaves the
   scheduler's fixed constants unscaled, and the pins race those as well — a read gives up after a
   fixed 0.10s slot wait no matter how large its scaled deadline, and a queued child is admitted on
   a fixed 0.20s stagger while its predecessor still runs. The second attempt missed this and moved
   the flake from py3.12 to py3.11 rather than fixing it.

So: import ``T`` and route EVERY duration in the file through it, and let this module stretch the
runtime's own constants to match. Each tests/test_*.py runs in its own process, so the patches below
cannot leak between files.
"""
from __future__ import annotations

import os

SCALE = float(os.environ.get("SLICE_TEST_TIME_SCALE") or 1.0)


def T(seconds: float) -> float:
    """Scale one timing budget. Every duration in a timing-sensitive test goes through here."""
    return seconds * SCALE


# Runtime constants the pins race but cannot pass as arguments. Read as module globals at call time,
# so rebinding them here is enough. Names are checked rather than assumed: a renamed constant must
# fail loudly instead of silently leaving that budget unscaled — a silently-unscaled constant is
# exactly the bug this module exists to prevent.
_RUNTIME_CLOCKS = {
    "sliceagent.scheduler": (
        "_LIFECYCLE_LAUNCH_STAGGER_SECONDS",   # queued-child admission
        "_TIMEOUT_POLL_SECONDS",               # wave poll interval
        "_TIMEOUT_GRACE_SECONDS",              # post-deadline settle window
        "_READER_SLOT_WAIT_SECONDS",           # reader-capacity wait before giving up
    ),
}


def stretch_runtime_clocks() -> None:
    """Scale the runtime's own fixed durations to match T(). No-op at scale 1.0."""
    if SCALE == 1.0:
        return
    import importlib

    for module_name, constants in _RUNTIME_CLOCKS.items():
        module = importlib.import_module(module_name)
        for name in constants:
            if not hasattr(module, name):
                raise AssertionError(
                    f"{module_name}.{name} no longer exists — it was a timing constant the pins "
                    f"race. Update _RUNTIME_CLOCKS, or a scaled run silently leaves it unscaled."
                )
            setattr(module, name, getattr(module, name) * SCALE)


stretch_runtime_clocks()
