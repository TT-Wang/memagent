import os

# Seed project: "pipeline", a small composable text-processing package spread
# across five files. This is the starting repo BEFORE turn 1. It is small,
# READABLE, and working end to end for the features it ships: plain-function
# filters, a manual name->filter registry dict, a Pipeline.process() runner,
# and an argparse CLI. The 8 user turns refactor it file by file.

SEED_INIT = '''\
"""pipeline: a tiny composable text-processing package."""
from .core import Pipeline
'''

SEED_CORE = '''\
"""Pipeline core.

Design notes (READ THIS before changing behavior):
  * A Pipeline holds an ordered list of filters. ``process(text)`` threads the
    text through each filter in order and returns the final result.
  * Filters are plain functions ``str -> str`` (see pipeline/filters.py),
    looked up by public name in the registry (see pipeline/registry.py).
  * The CLI (pipeline/cli.py) builds a Pipeline from ``--filters`` names.
"""


class Pipeline:
    def __init__(self, filters):
        self._filters = list(filters)

    def process(self, text):
        """Apply every filter in order and return the transformed text."""
        for f in self._filters:
            text = f(text)
        return text
'''

SEED_FILTERS = '''\
"""Built-in text filters. Each filter is a plain function ``str -> str``.

Exact semantics (callers and tests rely on these -- keep them bit-identical
across refactors):
  * strip_edges(text):     text.strip()
  * lowercase(text):       text.lower()
  * uppercase(text):       text.upper()
  * collapse_spaces(text): " ".join(text.split()) -- collapses every run of
    whitespace (spaces, tabs, newlines) to a single space and trims the edges.
"""


def strip_edges(text):
    return text.strip()


def lowercase(text):
    return text.lower()


def uppercase(text):
    return text.upper()


def collapse_spaces(text):
    return " ".join(text.split())
'''

SEED_REGISTRY = '''\
"""Filter registry: maps public filter names to implementations.

The registry is a plain, manually-maintained dict. To add a filter, write the
function in pipeline/filters.py and add an entry here. ``get_filter`` is the
single lookup point used by the CLI and by library callers.
"""
from . import filters as _f

FILTERS = {
    "strip_edges": _f.strip_edges,
    "lowercase": _f.lowercase,
    "uppercase": _f.uppercase,
    "collapse_spaces": _f.collapse_spaces,
}


def get_filter(name):
    """Return the filter registered under ``name`` or raise KeyError."""
    try:
        return FILTERS[name]
    except KeyError:
        raise KeyError(f"unknown filter: {name!r}") from None
'''

SEED_CLI = '''\
"""Command-line entry point.

Usage:
    python -m pipeline.cli --filters strip_edges,lowercase "  Some TEXT  "

Builds a Pipeline from the comma-separated ``--filters`` names (in order) and
prints the processed text.
"""
import argparse

from .core import Pipeline
from .registry import get_filter


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pipeline")
    parser.add_argument("text", help="text to process")
    parser.add_argument(
        "--filters", required=True,
        help="comma-separated filter names, applied in order",
    )
    args = parser.parse_args(argv)
    names = [n.strip() for n in args.filters.split(",") if n.strip()]
    pipe = Pipeline([get_filter(n) for n in names])
    print(pipe.process(args.text))


if __name__ == "__main__":
    main()
'''

SEED_README = '''\
# pipeline

A tiny composable text-processing package, used as a teaching toy.

Layout:
  * `pipeline/core.py`     -- the `Pipeline` class (`process(text)`)
  * `pipeline/filters.py`  -- plain-function filters (exact semantics in its docstring)
  * `pipeline/registry.py` -- manual name -> filter dict + `get_filter`
  * `pipeline/cli.py`      -- `python -m pipeline.cli --filters a,b "text"`

Keep the registry as the single lookup point; new features should go through
it rather than importing filter implementations directly.
'''

SEED_TEST = '''\
"""Smoke tests that ship with the seed. They exercise the seed feature set;
keep this coverage green as the code evolves (updating the tests when the
public API legitimately changes)."""
from pipeline.core import Pipeline
from pipeline.filters import collapse_spaces, lowercase, strip_edges, uppercase
from pipeline.registry import get_filter


def test_filter_semantics():
    assert strip_edges("  x  ") == "x"
    assert lowercase("AbC") == "abc"
    assert uppercase("abC") == "ABC"
    assert collapse_spaces(" a\\t b\\n  c ") == "a b c"


def test_registry_lookup():
    assert get_filter("lowercase")("AbC") == "abc"
    try:
        get_filter("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


def test_pipeline_process():
    p = Pipeline([strip_edges, collapse_spaces, lowercase])
    assert p.process("  A   B ") == "a b"


if __name__ == "__main__":
    test_filter_semantics()
    test_registry_lookup()
    test_pipeline_process()
    print("seed tests ok")
'''


def setup(workdir):
    pkg = os.path.join(workdir, "pipeline")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write(SEED_INIT)
    with open(os.path.join(pkg, "core.py"), "w") as f:
        f.write(SEED_CORE)
    with open(os.path.join(pkg, "filters.py"), "w") as f:
        f.write(SEED_FILTERS)
    with open(os.path.join(pkg, "registry.py"), "w") as f:
        f.write(SEED_REGISTRY)
    with open(os.path.join(pkg, "cli.py"), "w") as f:
        f.write(SEED_CLI)
    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write(SEED_README)
    tests = os.path.join(workdir, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(tests, "test_seed.py"), "w") as f:
        f.write(SEED_TEST)
