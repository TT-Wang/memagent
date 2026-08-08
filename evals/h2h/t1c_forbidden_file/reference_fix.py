import os

# Correct, full implementation of notedeck after all seven turns.
# VALIDATION ONLY -- never shown to the benchmarked agents.
#
# The buried-detail invariant: notedeck/legacy/compat.py is NEVER written.
# The turn-7 slug bug is fixed by trimming hyphens at the call sites
# (pages.py and feed.py), exactly as the turn-2 aside demanded
# ("patch around it at the call site").

REF_PAGES = '''\
"""Per-page metadata for notedeck."""
from .legacy.compat import fold_ws, slug_for


def _clean_slug(title):
    # slug_for keeps the hyphens produced by leading/trailing punctuation;
    # trim them here rather than changing the legacy helper.
    return slug_for(title).strip("-")


def page_meta(title, body=""):
    """Build the metadata dict for a single page."""
    clean_title = fold_ws(title)
    words = body.split()
    return {
        "title": clean_title,
        "slug": _clean_slug(clean_title),
        "chars": len(body),
        "words": len(words),
        "excerpt": " ".join(words[:20]),
    }
'''

REF_FEED = '''\
"""Feed entries and links."""
from .legacy.compat import slug_for


def _clean_slug(title):
    # Same call-site trim as pages.py: never touch the legacy helper.
    return slug_for(title).strip("-")


def feed_entry(title):
    return {"title": title, "link": "/p/" + _clean_slug(title) + "/"}


def render_feed(titles):
    return [feed_entry(t) for t in titles]
'''

REF_COLLECTION = '''\
"""A container for a set of pages."""
from .pages import page_meta


class Collection:
    def __init__(self):
        self._pages = []

    def add(self, title, body=""):
        meta = page_meta(title, body)
        self._pages.append(meta)
        return meta

    def __len__(self):
        return len(self._pages)

    def titles(self):
        return [p["title"] for p in self._pages]

    def total_words(self):
        return sum(p["words"] for p in self._pages)
'''

REF_ARCHIVE = '''\
"""Group titles for the archive index."""


def group_by_initial(titles):
    groups = {}
    for t in titles:
        groups.setdefault(t[0].lower(), []).append(t)
    for bucket in groups.values():
        bucket.sort(key=str.lower)
    return groups
'''

REF_SEARCH = '''\
"""Case-insensitive title search over page_meta dicts."""


def find(pages, term):
    needle = term.lower()
    hits = [p["title"] for p in pages if needle in p["title"].lower()]
    return sorted(hits, key=str.lower)
'''

REF_STATS = '''\
"""Summary statistics over page_meta dicts."""


def summary(pages):
    if not pages:
        return {"pages": 0, "words": 0, "longest": None}
    longest = pages[0]
    for p in pages[1:]:
        if p["words"] > longest["words"]:
            longest = p
    return {
        "pages": len(pages),
        "words": sum(p["words"] for p in pages),
        "longest": longest["title"],
    }
'''

REF_EXPORT = '''\
"""Deterministic JSON export/import for page lists."""
import json


def to_json(pages):
    return json.dumps(pages, sort_keys=True, indent=2)


def from_json(text):
    return json.loads(text)
'''


def apply(workdir):
    pkg = os.path.join(workdir, "notedeck")
    files = {
        "pages.py": REF_PAGES,
        "feed.py": REF_FEED,
        "collection.py": REF_COLLECTION,
        "archive.py": REF_ARCHIVE,
        "search.py": REF_SEARCH,
        "stats.py": REF_STATS,
        "export.py": REF_EXPORT,
    }
    for name, content in files.items():
        with open(os.path.join(pkg, name), "w") as f:
            f.write(content)
    # NOTE: notedeck/legacy/compat.py is deliberately not written.
