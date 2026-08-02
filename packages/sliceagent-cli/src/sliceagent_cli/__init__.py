"""sliceagent-cli — the SliceAgent coding agent, a CLI host over sliceagent-core.

Owns the coding tools, terminal UI, orchestration, and the Memem memory stack (injected
into the core via the Memory contract). Depends on sliceagent-core; the reverse never holds.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
