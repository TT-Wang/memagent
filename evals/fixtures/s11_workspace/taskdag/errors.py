"""Exceptions raised by taskdag."""


class CycleError(Exception):
    """Raised when a task registry contains a cycle."""
