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
