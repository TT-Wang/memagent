import os

# Seed project: confkit, a tiny config loader for flat KEY = VALUE files.
# This is the starting repo BEFORE turn 1. It is small and working for the
# features it has: every value is a raw string, keys are lowercased, blank
# lines are skipped, and anything else without an '=' is rejected. The 7
# user turns extend it (coercion, comments/errors, sections, validation,
# defaults, layering, env overrides).

SEED_LOADER = '''\
"""confkit: a tiny config loader for KEY = VALUE files.

Design notes (READ THIS before changing behavior):
  * A config file is a flat list of ``key = value`` lines.
  * Keys are stripped and lowercased; values are stripped raw strings.
  * Blank lines are skipped. Anything else without an ``=`` is rejected.
  * ``Config`` wraps the parsed dict; use ``get(key, default=None)``.

Public API (do not rename without updating callers):
  Config, Config.load(path), get(key, default=None)
"""


def _parse(text):
    data = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "=" not in stripped:
            raise ValueError("malformed config line: %r" % stripped)
        key, _, raw = stripped.partition("=")
        data[key.strip().lower()] = raw.strip()
    return data


class Config:
    def __init__(self, data=None):
        self._data = dict(data or {})

    @classmethod
    def load(cls, path):
        with open(path) as f:
            text = f.read()
        return cls(_parse(text))

    def get(self, key, default=None):
        return self._data.get(key, default)
'''

SEED_README = '''\
# confkit

A tiny config loader for flat `key = value` files, used as an internal tool.

Current capabilities:
  * `Config.load(path)` parses a file of `key = value` lines
  * every value comes back as a raw string
  * `get(key, default=None)` for lookups

See `confkit/loader.py` for the design notes. Keep `Config` and `get` as the
public surface; new features should extend the parser and the `Config` class
rather than replacing them.
'''

SEED_TEST = '''\
"""Smoke tests that ship with the seed. These exercise only the seed feature
set (flat string key/value files). Keep them green."""
import os
import tempfile

from confkit.loader import Config


def _write(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(text)
    return p


def test_parse_strings():
    with tempfile.TemporaryDirectory() as d:
        p = _write(d, "app.cfg", "name = demo\\ngreeting = hello world\\n")
        cfg = Config.load(p)
        assert cfg.get("name") == "demo"
        assert cfg.get("greeting") == "hello world"
        assert cfg.get("missing", "d") == "d"


def test_blank_lines_and_key_case():
    with tempfile.TemporaryDirectory() as d:
        p = _write(d, "app.cfg", "\\nOwner = tt\\n\\n")
        cfg = Config.load(p)
        assert cfg.get("owner") == "tt"


if __name__ == "__main__":
    test_parse_strings()
    test_blank_lines_and_key_case()
    print("seed tests ok")
'''


def setup(workdir):
    pkg = os.path.join(workdir, "confkit")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write("from .loader import Config\n")
    with open(os.path.join(pkg, "loader.py"), "w") as f:
        f.write(SEED_LOADER)
    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write(SEED_README)
    tests = os.path.join(workdir, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(tests, "test_seed.py"), "w") as f:
        f.write(SEED_TEST)
