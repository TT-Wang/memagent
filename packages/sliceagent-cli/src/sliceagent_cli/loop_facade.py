"""Legacy loop surface — a transparent alias of :mod:`sliceagent_core.loop`.

Core defaults the ordered scheduler itself (the scheduler is turn semantics and lives in
sliceagent_core.scheduler), so this facade's injection role dissolved. It remains only so
the legacy ``sliceagent.loop`` shim and older callers keep a stable import path — including
test imports of private loop names, forwarded via module __getattr__ (PEP 562).

NOTE: attribute READS forward to core; monkeypatch-style ASSIGNMENT must target
sliceagent_core.loop directly (the liveness pin already does, task #111).
New callers: import :mod:`sliceagent_core.loop` directly.
"""
from __future__ import annotations

from sliceagent_core import loop as _impl
from sliceagent_core.loop import run_tool_batch, run_turn  # noqa: F401 (stable public names)
from sliceagent_core.scheduler import ORDERED_TOOL_SCHEDULER  # noqa: F401 (legacy import site)


def __getattr__(name: str):
    return getattr(_impl, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_impl)))
