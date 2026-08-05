"""Task execution: run a registry's tasks in dependency order."""

from .config import get
from .graph import _as_dict, by_tag, topo_order, waves
from .log import log

#: The retry limit now lives in :mod:`taskdag.config` under the
#: ``qz_retry_limit`` key (session rule: exactly 3). ``RETRY_LIMIT``
#: remains available as a module-level read-through of that value, kept
#: for backwards compatibility; :func:`run` itself reads the config value
#: directly.


def __getattr__(name):
    """Fallback attribute access: ``RETRY_LIMIT`` reads through to config."""
    if name == "RETRY_LIMIT":
        return get("qz_retry_limit")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _tagged_closure(reg, tag):
    """Return the tasks carrying ``tag`` plus their dependency closure.

    ``reg`` is a plain dict (already normalized). The result is the set
    of names of the tasks that carry ``tag`` together with every task
    they depend on, directly or transitively -- the minimal set of tasks
    that must run for the tagged tasks to be executable.
    """
    selected = set(by_tag(reg, tag))
    frontier = list(selected)
    while frontier:
        name = frontier.pop()
        for dep in reg[name]:
            if dep not in selected:
                selected.add(dep)
                frontier.append(dep)
    return selected


def run(reg, fn, cancel=None, only_tag=None, key_fn=None, retry_limit=None):
    """Execute the tasks in ``reg`` by calling ``fn(name)`` for each.

    Tasks are processed in deterministic topological order. A task runs
    only when every one of its dependencies has completed successfully.
    ``cancel`` (default ``None``) is a set of task names to skip
    up-front: each such task is recorded under ``'skipped'`` without
    calling ``fn``, and every task that depends on one -- directly or
    transitively -- is skipped too. Names not present in ``reg`` are
    ignored. ``only_tag`` (default ``None``) restricts execution to the
    tasks carrying that tag plus every task they depend on (the
    transitive dependency closure); every other task is recorded under
    ``'skipped'`` without calling ``fn``. ``key_fn`` (default ``None``)
    is a deterministic seeding hook: it replaces the alphabetical
    tie-break among tasks that become ready at the same time with
    ``key_fn(name)`` order. ``retry_limit`` (default ``None``)
    overrides the configured ``qz_retry_limit`` for this call; ``None``
    keeps the config value.
    If ``fn`` raises for a task, that call is retried up to the
    effective retry limit (at most ``limit + 1`` attempts, where
    ``limit`` is ``retry_limit`` if given, else the configured
    ``qz_retry_limit``); only if every attempt raises is the task
    recorded under
    ``'failed'``, and
    every task that depends on it -- directly or transitively -- is then
    recorded under ``'skipped'`` without calling ``fn``. Tasks unrelated
    to the failure still run normally. Each task that runs is logged via
    :func:`taskdag.log.log`: ``task <name> start`` when it starts,
    ``task <name> done`` when it succeeds, and ``task <name> failed``
    once it exhausts its retries. ``reg`` may be a plain dict or a
    :class:`taskdag.registry.Registry` (its ``.deps`` dict is used).

    Returns ``{'done': [...], 'failed': [...], 'skipped': [...],
    'retries': {name: attempts}, 'workers': ..., 'wave_ms': [...]}``
    with each list in topological (execution) order; ``'retries'`` maps
    each failed task's name to the number of ``fn`` calls made for it
    (its retries plus the final failing attempt). ``'workers'`` mirrors
    the current ``worker_count`` config value (see
    :mod:`taskdag.config`). ``'wave_ms'`` is the simulated timing: one
    entry per wave, each equal to the configured ``wave_pause_ms``. A
    cyclic registry raises :class:`taskdag.graph.CycleError`.
    """
    reg = _as_dict(reg)
    cancelled = set(cancel) if cancel is not None else set()
    excluded = (
        set(reg) - _tagged_closure(reg, only_tag)
        if only_tag is not None
        else set()
    )
    if retry_limit is None:
        retry_limit = get("qz_retry_limit")
    done, failed, skipped = [], [], []
    retries = {}
    completed = set()
    for name in topo_order(reg, key_fn=key_fn):
        if name in cancelled or name in excluded:
            # Cancelled up-front or filtered out by only_tag: never runs;
            # dependents skip below because neither joins ``completed``.
            skipped.append(name)
            continue
        if not reg[name] <= completed:
            # Some dependency failed or was itself skipped.
            skipped.append(name)
            continue
        log("info", f"task {name} start")
        success = False
        attempts = 0
        for _ in range(retry_limit + 1):
            attempts += 1
            try:
                fn(name)
            except Exception:
                continue
            success = True
            break
        if not success:
            retries[name] = attempts
            log("error", f"task {name} failed")
            failed.append(name)
            continue
        log("info", f"task {name} done")
        done.append(name)
        completed.add(name)
    wave_ms = [get("wave_pause_ms")] * len(waves(reg, key_fn=key_fn))
    return {
        "done": done,
        "failed": failed,
        "skipped": skipped,
        "retries": retries,
        "workers": get("worker_count"),
        "wave_ms": wave_ms,
    }


def dry_run(reg, key_fn=None):
    """Return the wave plan for ``reg`` as text, one wave per line.

    ``reg`` is either a plain dict or a :class:`taskdag.registry.Registry`
    (its ``.deps`` dict is used). ``key_fn`` (default ``None``) is a
    deterministic seeding hook: it replaces the alphabetical tie-break
    within a wave with ``key_fn(name)`` order. Lines are numbered from 1
    and list that wave's tasks in priority order (higher first, then by
    ``key_fn``), e.g. ``wave 1: a, b``. Raises
    :class:`taskdag.graph.CycleError` for a cyclic registry.
    """
    reg = _as_dict(reg)
    return "\n".join(
        f"wave {i}: {', '.join(wave)}"
        for i, wave in enumerate(waves(reg, key_fn=key_fn), start=1)
    )
