import os
import shutil

# Correct, full implementation of the pipeline package after ALL eight turns.
# VALIDATION ONLY -- never shown to the benchmarked agents.
#
# Final state:
#   * Filter base class in pipeline/base.py (turn 1).
#   * Decorator-only registry with idempotent re-registration of the SAME
#     class and ValueError on a name conflict (turns 2 + 7); the manual
#     FILTERS dict and its DeprecationWarning fallback are gone (turn 8).
#   * Pipeline.run only -- the process() shim is deleted (turns 3 + 8).
#   * filters/ subpackage split by category (turn 4) whose __init__ imports
#     the category modules for registration side effects but re-exports NO
#     flat names (turn 8); the flat filters.py module is deleted.
#   * Config-driven CLI default chain from pipeline.json (turn 5).
#   * tokens category (turn 6).

REF_INIT = '''\
"""pipeline: a tiny composable text-processing package."""
from .core import Pipeline
from . import filters  # noqa: F401  -- imports category modules, registering all filters
'''

REF_BASE = '''\
"""Filter protocol.

Every filter is a subclass of :class:`Filter` that declares a class attribute
``name`` (its public registry name) and implements ``apply(self, text)``.
"""


class Filter:
    name = None

    def apply(self, text):
        raise NotImplementedError
'''

REF_REGISTRY = '''\
"""Filter registry: decorator-based registration, single lookup point.

``@register`` on a Filter subclass instantiates it and stores the instance
under ``cls.name``. Re-registering the SAME class is a harmless no-op (the
same module may legitimately be imported via more than one path); a DIFFERENT
class claiming an existing name raises ValueError.
"""

_REGISTRY = {}   # name -> Filter instance
_CLASSES = {}    # name -> owning class (for idempotency checks)


def register(cls):
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(f"filter class {cls!r} has no name")
    owner = _CLASSES.get(name)
    if owner is not None:
        if owner is cls:
            return cls  # idempotent re-registration of the same class
        raise ValueError(f"filter name {name!r} already registered")
    _CLASSES[name] = cls
    _REGISTRY[name] = cls()
    return cls


def get_filter(name):
    """Return the filter instance registered under ``name`` or raise KeyError."""
    try:
        return _REGISTRY[name]
    except KeyError:
        avail = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown filter {name!r}; available: {avail}") from None


def available():
    """Sorted list of all registered filter names."""
    return sorted(_REGISTRY)
'''

REF_CORE = '''\
"""Pipeline core.

A Pipeline holds an ordered list of Filter instances; ``run(text)`` threads
the text through each filter's ``apply`` in order. Build from registry names
with :meth:`Pipeline.from_names`.
"""


class Pipeline:
    def __init__(self, filters):
        self._filters = list(filters)

    def run(self, text):
        """Apply every filter in order and return the transformed text."""
        for f in self._filters:
            text = f.apply(text)
        return text

    @classmethod
    def from_names(cls, names):
        """Build a Pipeline by looking each name up in the registry."""
        from .registry import get_filter
        return cls([get_filter(n) for n in names])
'''

REF_FILTERS_INIT = '''\
"""Filter categories.

Importing this package imports every category module so that their
``@register`` decorators run -- the registry is fully populated as a side
effect of ``import pipeline`` / ``import pipeline.filters``.

Filters are looked up through the registry (``pipeline.registry.get_filter``)
or imported from their category module; there are no flat re-exports here.
"""
from . import spacing, text, tokens  # noqa: F401
'''

REF_FILTERS_TEXT = '''\
"""Case-transform filters."""
from ..base import Filter
from ..registry import register


@register
class Lowercase(Filter):
    name = "lowercase"

    def apply(self, text):
        return text.lower()


@register
class Uppercase(Filter):
    name = "uppercase"

    def apply(self, text):
        return text.upper()
'''

REF_FILTERS_SPACING = '''\
"""Whitespace filters."""
from ..base import Filter
from ..registry import register


@register
class StripEdges(Filter):
    name = "strip_edges"

    def apply(self, text):
        return text.strip()


@register
class CollapseSpaces(Filter):
    name = "collapse_spaces"

    def apply(self, text):
        return " ".join(text.split())
'''

REF_FILTERS_TOKENS = '''\
"""Word-level filters."""
from ..base import Filter
from ..registry import register


@register
class DedupeWords(Filter):
    name = "dedupe_words"

    def apply(self, text):
        seen = set()
        out = []
        for w in text.split():
            if w not in seen:
                seen.add(w)
                out.append(w)
        return " ".join(out)


@register
class SortWords(Filter):
    name = "sort_words"

    def apply(self, text):
        return " ".join(sorted(text.split()))
'''

REF_CLI = '''\
"""Command-line entry point.

Usage:
    python -m pipeline.cli --filters strip_edges,lowercase "  Some TEXT  "
    python -m pipeline.cli "  Some TEXT  "            # uses pipeline.json
    python -m pipeline.cli --config other.json "..."

When ``--filters`` is omitted, the default chain comes from the JSON config
file (``--config``, default ./pipeline.json): {"default_chain": [names...]}.
"""
import argparse
import json
import os

from .core import Pipeline


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pipeline")
    parser.add_argument("text", help="text to process")
    parser.add_argument(
        "--filters",
        help="comma-separated filter names, applied in order "
             "(overrides the config default chain)",
    )
    parser.add_argument(
        "--config", default="pipeline.json",
        help="JSON config file providing default_chain (default: pipeline.json)",
    )
    args = parser.parse_args(argv)
    if args.filters:
        names = [n.strip() for n in args.filters.split(",") if n.strip()]
    else:
        if not os.path.isfile(args.config):
            parser.error(
                f"no --filters given and config file {args.config!r} not found"
            )
        with open(args.config) as f:
            names = json.load(f)["default_chain"]
    pipe = Pipeline.from_names(names)
    print(pipe.run(args.text))


if __name__ == "__main__":
    main()
'''

REF_CONFIG = '''\
{
  "default_chain": ["strip_edges", "collapse_spaces", "lowercase"]
}
'''

REF_TEST = '''\
"""Smoke tests, updated for the post-cleanup API (run(), registry only)."""
from pipeline.core import Pipeline
from pipeline.registry import available, get_filter


def test_filter_semantics():
    assert get_filter("strip_edges").apply("  x  ") == "x"
    assert get_filter("lowercase").apply("AbC") == "abc"
    assert get_filter("uppercase").apply("abC") == "ABC"
    assert get_filter("collapse_spaces").apply(" a\\t b\\n  c ") == "a b c"


def test_registry_lookup():
    assert "dedupe_words" in available()
    try:
        get_filter("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


def test_pipeline_run():
    p = Pipeline.from_names(["strip_edges", "collapse_spaces", "lowercase"])
    assert p.run("  A   B ") == "a b"


if __name__ == "__main__":
    test_filter_semantics()
    test_registry_lookup()
    test_pipeline_run()
    print("seed tests ok")
'''


def apply(workdir):
    pkg = os.path.join(workdir, "pipeline")
    # The flat filters module was replaced by the filters/ subpackage.
    flat = os.path.join(pkg, "filters.py")
    if os.path.isfile(flat):
        os.remove(flat)
    cache = os.path.join(pkg, "__pycache__")
    if os.path.isdir(cache):
        shutil.rmtree(cache)
    sub = os.path.join(pkg, "filters")
    os.makedirs(sub, exist_ok=True)

    files = {
        os.path.join(pkg, "__init__.py"): REF_INIT,
        os.path.join(pkg, "base.py"): REF_BASE,
        os.path.join(pkg, "registry.py"): REF_REGISTRY,
        os.path.join(pkg, "core.py"): REF_CORE,
        os.path.join(pkg, "cli.py"): REF_CLI,
        os.path.join(sub, "__init__.py"): REF_FILTERS_INIT,
        os.path.join(sub, "text.py"): REF_FILTERS_TEXT,
        os.path.join(sub, "spacing.py"): REF_FILTERS_SPACING,
        os.path.join(sub, "tokens.py"): REF_FILTERS_TOKENS,
        os.path.join(workdir, "pipeline.json"): REF_CONFIG,
        os.path.join(workdir, "tests", "test_seed.py"): REF_TEST,
    }
    os.makedirs(os.path.join(workdir, "tests"), exist_ok=True)
    for path, content in files.items():
        with open(path, "w") as f:
            f.write(content)
