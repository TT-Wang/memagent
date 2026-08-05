"""A :class:`Registry` wrapper around the plain dict registry."""

from .graph import add_task, remove_task


class Registry:
    """Wrap the plain ``{name: set(dependencies)}`` dict registry.

    The underlying dict is exposed as :attr:`deps`; :meth:`add` and
    :meth:`remove` delegate to the module-level
    :func:`taskdag.graph.add_task` / :func:`taskdag.graph.remove_task`
    functions. Every ``taskdag`` function that takes a registry also
    accepts a :class:`Registry` in place of a plain dict.
    """

    def __init__(self):
        self.deps = {}

    def add(self, name, depends_on=(), priority=0, tags=()):
        """Register ``name``; see :func:`taskdag.graph.add_task`."""
        add_task(
            self.deps,
            name,
            depends_on=depends_on,
            priority=priority,
            tags=tags,
        )

    def remove(self, name):
        """Remove ``name``; see :func:`taskdag.graph.remove_task`."""
        remove_task(self.deps, name)
