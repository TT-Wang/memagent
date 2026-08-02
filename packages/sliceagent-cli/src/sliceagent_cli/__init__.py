"""sliceagent-cli — the SliceAgent coding agent, a CLI host over sliceagent-core.

Owns the coding tools, terminal UI, orchestration, and the Memem memory stack (injected
into the core via the Memory contract). Depends on sliceagent-core; the reverse never holds.
"""

__version__ = "0.1.0"
def app_version() -> str:
    """The installed PRODUCT version (the `sliceagent` distribution, single-sourced from
    src/sliceagent/__init__.py) with a standalone fallback to this package's own version.
    Metadata-based on purpose: no import coupling to the umbrella package."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("sliceagent")
    except PackageNotFoundError:
        return __version__


__all__ = ["__version__", "app_version"]
