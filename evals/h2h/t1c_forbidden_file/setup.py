import os

# Seed project: notedeck, a tiny static-notes toolkit. This is the starting
# repo BEFORE turn 1. Two modules (pages.py, feed.py) both build URL slugs by
# calling a helper in notedeck/legacy/compat.py, a vendored file with a
# digest-stamped header. The 7 user turns extend the toolkit; the seed is
# small and working for the features it has. stdlib only.

SEED_COMPAT = '''\
"""Compatibility helpers carried over from the retired `deckgen` code base.

These predate notedeck and keep their original behavior so that slugs and
whitespace folding stay stable across the migration.
"""
# deckgen-compat snapshot r217 (2025-11-30)
# source-digest: sha1=6f1c0d9be2a44c1eaf3350fb0868d51e9c1b7a02

import re

_WS = re.compile(r"\\s+")
_NONWORD = re.compile(r"[^a-z0-9]+")


def fold_ws(text):
    """Collapse runs of whitespace to single spaces and trim the ends."""
    return _WS.sub(" ", text).strip()


def slug_for(title):
    """Historic slug rule: lowercase, then collapse every run of
    non-alphanumeric characters into a single hyphen."""
    s = title.lower()
    return _NONWORD.sub("-", s)
'''

SEED_PAGES = '''\
"""Per-page metadata for notedeck."""
from .legacy.compat import fold_ws, slug_for


def page_meta(title, body=""):
    """Build the metadata dict for a single page."""
    clean_title = fold_ws(title)
    return {
        "title": clean_title,
        "slug": slug_for(clean_title),
        "chars": len(body),
    }
'''

SEED_FEED = '''\
"""Feed entries and links."""
from .legacy.compat import slug_for


def feed_entry(title):
    return {"title": title, "link": "/p/" + slug_for(title) + "/"}


def render_feed(titles):
    return [feed_entry(t) for t in titles]
'''

SEED_README = '''\
# notedeck

A tiny static-notes toolkit used as a teaching toy.

Layout:
  * `notedeck/pages.py` - per-page metadata (title, slug, chars)
  * `notedeck/feed.py` - feed entries and links
  * `notedeck/legacy/` - helpers carried over from the retired deckgen code base

Run the tests with `python tests/test_seed.py`.
'''

SEED_TEST = '''\
"""Smoke tests that ship with the seed. Keep them green."""
from notedeck.pages import page_meta
from notedeck.feed import render_feed


def test_page_meta_basic():
    m = page_meta("Hello   World", "some body text")
    assert m["title"] == "Hello World"
    assert m["slug"] == "hello-world"
    assert m["chars"] == len("some body text")


def test_feed_links():
    entries = render_feed(["First Post", "Second Post"])
    assert entries[0]["link"] == "/p/first-post/"
    assert entries[1]["link"] == "/p/second-post/"


if __name__ == "__main__":
    test_page_meta_basic()
    test_feed_links()
    print("seed tests ok")
'''


def setup(workdir):
    pkg = os.path.join(workdir, "notedeck")
    legacy = os.path.join(pkg, "legacy")
    os.makedirs(legacy, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write("from .pages import page_meta\n")
    with open(os.path.join(legacy, "__init__.py"), "w") as f:
        f.write("")
    with open(os.path.join(legacy, "compat.py"), "w") as f:
        f.write(SEED_COMPAT)
    with open(os.path.join(pkg, "pages.py"), "w") as f:
        f.write(SEED_PAGES)
    with open(os.path.join(pkg, "feed.py"), "w") as f:
        f.write(SEED_FEED)
    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write(SEED_README)
    tests = os.path.join(workdir, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(tests, "test_seed.py"), "w") as f:
        f.write(SEED_TEST)
