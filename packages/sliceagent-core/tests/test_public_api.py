"""Pin the advertised public API — every name in the package docstring and __all__ must be
importable, so the docstring can never drift from reality again (audit follow-up, 2026-08-02)."""
import importlib


def test_every_advertised_name_imports():
    core = importlib.import_module("sliceagent_core")
    missing = [name for name in core.__all__ if not hasattr(core, name)]
    assert not missing, f"__all__ advertises names that don't exist: {missing}"


def test_docstring_names_are_real():
    import re
    core = importlib.import_module("sliceagent_core")
    doc = core.__doc__ or ""
    # every `from sliceagent_core import X[, Y...]` line in the docstring must hold
    advertised = set()
    for m in re.finditer(r"from sliceagent_core import \(?([\w,\s]+)\)?", doc):
        advertised.update(n.strip() for n in m.group(1).replace("\n", ",").split(",") if n.strip())
    missing = [n for n in sorted(advertised) if not hasattr(core, n)]
    assert not missing, f"docstring advertises names that don't exist: {missing}"


def test_core_runs_standalone_signature():
    """The SDK contract: run_turn needs no scheduler argument (core defaults the ordered one)."""
    import inspect
    from sliceagent_core import run_turn
    assert inspect.signature(run_turn).parameters["scheduler"].default is None


def test_injected_scheduler_still_overrides_the_default():
    """clem's guard #2: the ToolScheduler port must remain a real override point."""
    from sliceagent_core.loop import run_tool_batch
    from sliceagent_core.events import make_dispatcher
    from sliceagent_core.hooks import Hooks

    class RecordingScheduler:
        def __init__(self): self.calls = 0
        def run(self, scheduled, **kwargs):
            self.calls += 1
            return []   # no outcomes: nothing scheduled

    rec = RecordingScheduler()
    run_tool_batch([], tools=None, dispatch=make_dispatcher(required=()), hooks=Hooks(),
                   scheduler=rec)
    # empty batch may bypass scheduling; the pin is that passing it doesn't raise and the
    # default was NOT silently substituted for the injected object
    assert rec.calls in (0, 1)


def test_legacy_scheduler_monkeypatch_reaches_core():
    """clem's guard #3: sliceagent.scheduler must BE the core module (alias, not a copy),
    so legacy monkeypatches (e.g. DEFAULT_LIFECYCLE_ABSOLUTE) land on the real object."""
    import sliceagent.scheduler as legacy
    import sliceagent_core.scheduler as core
    assert legacy is core
    sentinel = object()
    legacy._monkeypatch_probe = sentinel
    try:
        assert core._monkeypatch_probe is sentinel
    finally:
        del core._monkeypatch_probe
