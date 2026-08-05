"""Task DAG operations over a plain dict registry ``{name: set(dependencies)}``.

Every function here accepts either a plain dict or a
:class:`taskdag.registry.Registry` (whose ``.deps`` dict is used).
"""

import heapq

from .errors import CycleError  # re-exported for backwards compatibility
from .validate import validate_dependencies


def _as_dict(reg):
    """Return the plain dict underlying ``reg``.

    ``reg`` may be a plain dict or a :class:`taskdag.registry.Registry`
    whose ``.deps`` attribute holds one; anything else raises
    :class:`TypeError`.
    """
    if isinstance(reg, dict):
        return reg
    deps = getattr(reg, "deps", None)
    if isinstance(deps, dict):
        return deps
    raise TypeError(
        f"expected a dict or Registry, got {type(reg).__name__!r}"
    )


class _Task(set):
    """A dependency set that also carries scheduling priority and tags.

    Values stay ordinary sets (``==``, ``<=``, iteration all behave the
    same), so the plain-dict registry contract ``{name: set(deps)}`` is
    unchanged; ``priority`` and ``tags`` are carried as attributes for
    wave ordering and :func:`by_tag` lookups.
    """

    def __init__(self, deps=(), priority=0, tags=()):
        super().__init__(deps)
        self.priority = priority
        self.tags = frozenset(tags)


def add_task(reg, name, depends_on=(), priority=0, tags=()):
    """Register ``name`` in ``reg`` with the given dependencies.

    ``reg`` is either a plain dict or a :class:`taskdag.registry.Registry`
    (its ``.deps`` dict is used). Dependencies must already exist in the
    registry; a task may not depend on itself. Violations raise
    :class:`ValueError` (checked by
    :func:`taskdag.validate.validate_dependencies`). ``priority`` (default
    0) orders the task within its wave in :func:`waves` / :func:`dry_run`:
    higher priorities run first, ties break alphabetically. ``tags``
    (default ``()``) attaches a set of string labels to the task, used by
    :func:`by_tag`.
    """
    reg = _as_dict(reg)
    deps = set(depends_on)
    validate_dependencies(reg, name, deps)
    reg[name] = _Task(deps, priority, tags)


def remove_task(reg, name):
    """Remove ``name`` from ``reg`` and strip it from other tasks' deps.

    ``reg`` is either a plain dict or a
    :class:`taskdag.registry.Registry` (its ``.deps`` dict is used).
    """
    reg = _as_dict(reg)
    del reg[name]
    for deps in reg.values():
        deps.discard(name)


def by_tag(reg, tag):
    """Return the names of tasks in ``reg`` carrying ``tag``, sorted.

    ``reg`` is either a plain dict or a :class:`taskdag.registry.Registry`
    (its ``.deps`` dict is used). A task carries ``tag`` if it was
    registered with it via :func:`add_task`'s ``tags`` argument; tasks in
    hand-built plain-set registries carry no tags. Returns an
    alphabetically sorted list of matching task names (``[]`` if none).
    """
    reg = _as_dict(reg)
    return sorted(
        name for name, deps in reg.items() if tag in getattr(deps, "tags", ())
    )


def merge(a, b):
    """Return a new registry that is the union of registries ``a`` and ``b``.

    ``a`` and ``b`` are either plain dicts or
    :class:`taskdag.registry.Registry` instances (their ``.deps`` dicts
    are used); neither input is mutated. The result is a plain-dict
    registry whose task names are the union of the two inputs' names,
    with each dependency set copied over. A name present in both must
    have the identical dependency set in each -- otherwise
    :class:`ValueError` is raised naming the task and the two conflicting
    sets. For a name present in both with equal dependency sets, the
    entry from ``a`` (including its ``priority`` and ``tags`` attributes)
    is kept.
    """
    a = _as_dict(a)
    b = _as_dict(b)
    merged = {}
    for name, deps in a.items():
        merged[name] = _Task(
            set(deps), getattr(deps, "priority", 0), getattr(deps, "tags", ())
        )
    for name, deps in b.items():
        if name in merged:
            if merged[name] != deps:
                raise ValueError(
                    f"conflicting dependency sets for task {name!r}: "
                    f"{sorted(merged[name])} vs {sorted(deps)}"
                )
            continue
        merged[name] = _Task(
            set(deps), getattr(deps, "priority", 0), getattr(deps, "tags", ())
        )
    return merged


def _find_cycle(reg):
    """Return one concrete cycle path (a list of names) in ``reg`` or ``None``.

    ``reg`` is a plain dict (already normalized). A cycle path is a
    sequence ``[n0, n1, ..., nk]`` where every task depends on the next
    (``n[i+1]`` is a dependency of ``n[i]``) and the final name repeats
    the first (``nk == n0``), e.g. ``["a", "b", "a"]``. Uses
    depth-first search over the ``depends on`` edges with
    white/gray/black coloring: a back edge into a gray ancestor is one
    concrete cycle. Deterministic: starting names and each task's
    dependencies are visited in sorted order. Returns ``None`` for an
    acyclic registry.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in reg}
    path = []

    def visit(name):
        color[name] = GRAY
        path.append(name)
        for dep in sorted(reg[name]):
            if color[dep] == GRAY:
                start = path.index(dep)
                return path[start:] + [dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found is not None:
                    return found
        path.pop()
        color[name] = BLACK
        return None

    for name in sorted(reg):
        if color[name] == WHITE:
            found = visit(name)
            if found is not None:
                return found
    return None


def topo_order(reg, key_fn=None):
    """Return a deterministic topological order of ``reg``'s tasks.

    ``reg`` is either a plain dict or a :class:`taskdag.registry.Registry`
    (its ``.deps`` dict is used). Uses Kahn's algorithm; tasks that become
    ready at the same time are emitted in ``key_fn(name)`` order, or
    alphabetically when ``key_fn`` is ``None``. Raises
    :class:`CycleError` if the registry contains a cycle; the
    exception message names one concrete cycle path such as
    ``a -> b -> a``.
    """
    reg = _as_dict(reg)
    indegree = {name: len(deps) for name, deps in reg.items()}
    dependents = {name: [] for name in reg}
    for name, deps in reg.items():
        for dep in deps:
            dependents[dep].append(name)

    key = key_fn if key_fn is not None else lambda name: name
    ready = [(key(name), name) for name, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)

    order = []
    while ready:
        _, name = heapq.heappop(ready)
        order.append(name)
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, (key(dependent), dependent))

    if len(order) != len(reg):
        raise CycleError(
            f"cycle detected in task registry: {' -> '.join(_find_cycle(reg))}"
        )
    return order


def waves(reg, key_fn=None):
    """Group ``reg``'s tasks into parallel waves.

    ``reg`` is either a plain dict or a :class:`taskdag.registry.Registry`
    (its ``.deps`` dict is used). Wave 0 holds the tasks with no
    dependencies; every later wave holds the tasks whose dependencies all
    appear in strictly earlier waves, so one wave's tasks can run in
    parallel. Within a wave, tasks are sorted by priority (higher first),
    then by ``key_fn(name)`` -- alphabetical when ``key_fn`` is ``None``.
    Raises :class:`CycleError` if the registry contains a cycle (a cycle
    never becomes ready, so it cannot be scheduled; the exception
    message names one concrete cycle path such as ``a -> b -> a``).
    """
    reg = _as_dict(reg)
    indegree = {name: len(deps) for name, deps in reg.items()}
    dependents = {name: [] for name in reg}
    for name, deps in reg.items():
        for dep in deps:
            dependents[dep].append(name)

    ready = [name for name, degree in indegree.items() if degree == 0]
    key = key_fn if key_fn is not None else lambda name: name
    result = []
    while ready:
        wave = sorted(
            ready,
            key=lambda name: (-getattr(reg[name], "priority", 0), key(name)),
        )
        result.append(wave)
        next_ready = []
        for name in wave:
            for dependent in dependents[name]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    next_ready.append(dependent)
        ready = next_ready

    if sum(len(wave) for wave in result) != len(reg):
        raise CycleError(
            f"cycle detected in task registry: {' -> '.join(_find_cycle(reg))}"
        )
    return result
