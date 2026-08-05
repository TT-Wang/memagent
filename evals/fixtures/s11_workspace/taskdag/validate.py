"""Validation helpers for taskdag registry operations."""


def validate_dependencies(reg, name, deps):
    """Validate the dependencies for a task about to be registered.

    ``deps`` must be a collection (typically the ``set`` built by
    :func:`taskdag.graph.add_task`). Raises :class:`ValueError` if
    ``name`` is among its own dependencies or if any dependency is not
    already present in ``reg``.
    """
    if name in deps:
        raise ValueError(f"task {name!r} cannot depend on itself")
    unknown = deps - set(reg)
    if unknown:
        raise ValueError(
            f"unknown dependencies for task {name!r}: "
            + ", ".join(repr(d) for d in sorted(unknown))
        )
