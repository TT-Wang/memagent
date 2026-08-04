import os

# Correct, full implementation of the notes CLI after all eight turns.
# VALIDATION ONLY -- never shown to the benchmarked agents.
#
# The load-bearing separation that makes turns 6-8 come out right:
#   * ``normalize_tag`` (turn 3's lowercase + strip + whitespace-runs -> '-')
#     is REMOVED entirely at turn 6 (the prompt demands a clean removal, no
#     dead helper left behind) and reintroduced from recall at turn 8, where
#     it is applied ONLY when explicitly opted in (--normalize-tags). This
#     file is the post-turn-8 final state, which is why the helper is present
#     here. Writes and list --tag are raw/exact by default.
#   * ``search`` (turn 5) never depended on write-side normalization for its
#     case-insensitivity: it lowercases both sides at query time, so the
#     revert leaves it intact and legacy normalized tags stay findable.

REFERENCE_STORE = '''\
"""A tiny JSONL-backed notes store.

Design notes:
  * Notes live in a JSON-Lines file: one JSON object per line, in insertion
    order. Record shape: {"id": int, "title": str, "body": str,
    "tags": list[str], "created_at": ISO-8601 str, "archived": bool}.
    Readers tolerate records written before a field existed.
  * ids are integers assigned as (max existing id) + 1, starting at 1.
  * Tags are stored EXACTLY as typed. ``normalize_tag`` is the historical
    cleanup (lowercase, strip, internal whitespace runs -> '-'); it is applied
    only when explicitly requested (normalize=True, i.e. --normalize-tags).
  * ``search`` matches case-insensitively against title/body substrings and
    against whole tags; it lowercases both sides itself and does not rely on
    tags having been normalized at write time.
"""
import json
import os
import re
from datetime import datetime, timezone


def normalize_tag(tag):
    """Historical tag cleanup: lowercase, strip, whitespace runs -> '-'."""
    return re.sub(r"\\s+", "-", tag.strip()).lower()


class NoteStore:
    def __init__(self, path):
        self.path = path

    # ----- persistence ----------------------------------------------------
    def _load(self):
        if not os.path.exists(self.path):
            return []
        notes = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    notes.append(json.loads(line))
        return notes

    def _save(self, notes):
        with open(self.path, "w", encoding="utf-8") as f:
            for n in notes:
                f.write(json.dumps(n, ensure_ascii=False) + "\\n")

    @staticmethod
    def _visible(notes, include_archived):
        if include_archived:
            return list(notes)
        return [n for n in notes if not n.get("archived")]

    # ----- operations -----------------------------------------------------
    def add(self, title, body, tags=None, normalize=False):
        notes = self._load()
        nid = max((n["id"] for n in notes), default=0) + 1
        tags = list(tags or [])
        if normalize:
            tags = [normalize_tag(t) for t in tags]
        notes.append({
            "id": nid,
            "title": title,
            "body": body,
            "tags": tags,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "archived": False,
        })
        self._save(notes)
        return nid

    def list_notes(self, tag=None, since=None, include_archived=False,
                   normalize=False):
        out = []
        for n in self._visible(self._load(), include_archived):
            if tag is not None:
                query = normalize_tag(tag) if normalize else tag
                if query not in n.get("tags", []):
                    continue
            if since is not None:
                raw = n.get("created_at")
                if not raw:
                    continue
                created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if created.date() < since:
                    continue
            out.append(n)
        return out

    def find(self, text, include_archived=False):
        return [
            n for n in self._visible(self._load(), include_archived)
            if text in n["title"] or text in n["body"]
        ]

    def search(self, text, include_archived=False):
        q = text.lower()
        out = []
        for n in self._visible(self._load(), include_archived):
            if q in n["title"].lower() or q in n["body"].lower():
                out.append(n)
            elif any(q == t.lower() for t in n.get("tags", [])):
                out.append(n)
        return out

    def set_archived(self, nid, archived):
        notes = self._load()
        for n in notes:
            if n["id"] == nid:
                n["archived"] = archived
                self._save(notes)
                return
        raise KeyError(nid)

    def rename_tag(self, old, new):
        """Exact raw match on ``old``; ``new`` is stored verbatim."""
        notes = self._load()
        count = 0
        for n in notes:
            tags = n.get("tags", [])
            if old in tags:
                n["tags"] = [new if t == old else t for t in tags]
                count += 1
        self._save(notes)
        return count
'''

REFERENCE_CLI = '''\
"""Command-line interface for the notes store.

Usage:
    python -m notes.cli [--file NOTES.jsonl] add [--tag T]... [--normalize-tags] TITLE BODY
    python -m notes.cli [--file NOTES.jsonl] list [--tag T] [--since YYYY-MM-DD] [--all] [--normalize-tags]
    python -m notes.cli [--file NOTES.jsonl] find TEXT [--all]
    python -m notes.cli [--file NOTES.jsonl] search TEXT [--all]
    python -m notes.cli [--file NOTES.jsonl] archive ID
    python -m notes.cli [--file NOTES.jsonl] unarchive ID
    python -m notes.cli [--file NOTES.jsonl] rename-tag OLD NEW

Design notes:
  * ``--file`` (default: notes.jsonl) stays the supported way to point the
    CLI at a different notes file.
  * Notes print one per line as "[<id>] <title> :: <body>".
  * Tags are raw by default; ``--normalize-tags`` opts add/list into the
    historical normalization.
"""
import argparse
from datetime import date

from .store import NoteStore


def print_notes(notes):
    for n in notes:
        print(f"[{n['id']}] {n['title']} :: {n['body']}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="notes")
    parser.add_argument("--file", default="notes.jsonl")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="add a note")
    p_add.add_argument("title")
    p_add.add_argument("body")
    p_add.add_argument("--tag", action="append", default=[])
    p_add.add_argument("--normalize-tags", action="store_true")

    p_list = sub.add_parser("list", help="list notes")
    p_list.add_argument("--tag")
    p_list.add_argument("--since")
    p_list.add_argument("--all", action="store_true")
    p_list.add_argument("--normalize-tags", action="store_true")

    p_find = sub.add_parser("find", help="find notes by substring")
    p_find.add_argument("text")
    p_find.add_argument("--all", action="store_true")

    p_search = sub.add_parser("search", help="case-insensitive search")
    p_search.add_argument("text")
    p_search.add_argument("--all", action="store_true")

    p_arch = sub.add_parser("archive", help="archive a note")
    p_arch.add_argument("id", type=int)

    p_unarch = sub.add_parser("unarchive", help="unarchive a note")
    p_unarch.add_argument("id", type=int)

    p_ren = sub.add_parser("rename-tag", help="rename a tag (raw, exact)")
    p_ren.add_argument("old")
    p_ren.add_argument("new")

    args = parser.parse_args(argv)
    store = NoteStore(args.file)

    if args.cmd == "add":
        nid = store.add(args.title, args.body, tags=args.tag,
                        normalize=args.normalize_tags)
        print(f"added {nid}")
    elif args.cmd == "list":
        since = date.fromisoformat(args.since) if args.since else None
        print_notes(store.list_notes(tag=args.tag, since=since,
                                     include_archived=args.all,
                                     normalize=args.normalize_tags))
    elif args.cmd == "find":
        print_notes(store.find(args.text, include_archived=args.all))
    elif args.cmd == "search":
        print_notes(store.search(args.text, include_archived=args.all))
    elif args.cmd == "archive":
        store.set_archived(args.id, True)
        print(f"archived {args.id}")
    elif args.cmd == "unarchive":
        store.set_archived(args.id, False)
        print(f"unarchived {args.id}")
    elif args.cmd == "rename-tag":
        count = store.rename_tag(args.old, args.new)
        print(f"renamed tags on {count} notes")


if __name__ == "__main__":
    main()
'''


def apply(workdir):
    pkg = os.path.join(workdir, "notes")
    with open(os.path.join(pkg, "store.py"), "w") as f:
        f.write(REFERENCE_STORE)
    with open(os.path.join(pkg, "cli.py"), "w") as f:
        f.write(REFERENCE_CLI)
