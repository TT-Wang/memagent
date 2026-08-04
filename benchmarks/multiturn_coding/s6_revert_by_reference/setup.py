import os

# Seed project: a tiny JSONL-backed notes CLI with add/list/find only.
# This is the starting repo BEFORE turn 1. It is small and working for the
# features it has. The 8 user turns extend it (tags, timestamps, tag
# normalization, archiving, search, a revert of the normalization, rename-tag,
# and an opt-in normalization flag). Stdlib only.

SEED_STORE = '''\
"""A tiny JSONL-backed notes store.

Design notes (READ THIS before changing behavior):
  * Notes live in a JSON-Lines file: one JSON object per line, in insertion
    order. The seed record shape is {"id": int, "title": str, "body": str}.
    New features should ADD fields to this record rather than replacing the
    format, and readers must tolerate records written before a field existed.
  * ids are integers assigned as (max existing id) + 1, starting at 1.
  * The whole file is small; loading it fully and rewriting it on mutation is
    the intended style. No indexes, no partial writes.

Public API (the CLI in cli.py is the supported interface; keep its commands
and its --file option working):
  NoteStore(path).add(title, body) -> id
  NoteStore(path).list_notes() -> list[dict]
  NoteStore(path).find(text) -> list[dict]   # substring in title or body
"""
import json
import os


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

    # ----- operations -----------------------------------------------------
    def add(self, title, body):
        notes = self._load()
        nid = max((n["id"] for n in notes), default=0) + 1
        notes.append({"id": nid, "title": title, "body": body})
        self._save(notes)
        return nid

    def list_notes(self):
        return self._load()

    def find(self, text):
        return [
            n for n in self._load()
            if text in n["title"] or text in n["body"]
        ]
'''

SEED_CLI = '''\
"""Command-line interface for the notes store.

Usage:
    python -m notes.cli [--file NOTES.jsonl] add TITLE BODY
    python -m notes.cli [--file NOTES.jsonl] list
    python -m notes.cli [--file NOTES.jsonl] find TEXT

Design notes:
  * ``--file`` (default: notes.jsonl in the current directory) is the
    supported way to point the CLI at a different notes file; keep it working.
  * Notes print one per line as "[<id>] <title> :: <body>"; keep the
    one-line-per-note shape so output stays grep-able.
"""
import argparse

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

    sub.add_parser("list", help="list notes")

    p_find = sub.add_parser("find", help="find notes by substring")
    p_find.add_argument("text")

    args = parser.parse_args(argv)
    store = NoteStore(args.file)

    if args.cmd == "add":
        nid = store.add(args.title, args.body)
        print(f"added {nid}")
    elif args.cmd == "list":
        print_notes(store.list_notes())
    elif args.cmd == "find":
        print_notes(store.find(args.text))


if __name__ == "__main__":
    main()
'''

SEED_README = '''\
# notes

A tiny JSONL-backed note-taking CLI, used as a teaching toy.

Current capabilities:

    python -m notes.cli add "title" "body"     # append a note
    python -m notes.cli list                   # show all notes
    python -m notes.cli find TEXT              # substring match in title/body

Notes are stored one-JSON-object-per-line in `notes.jsonl` (override with
`--file`). See `notes/store.py` for the design notes. New features should add
fields to the note record rather than changing the storage format.
'''

SEED_TEST = '''\
"""Smoke tests that ship with the seed. These exercise only the seed feature
set (add/list/find). Keep them green."""
import os
import tempfile

from notes.store import NoteStore


def test_add_and_list():
    with tempfile.TemporaryDirectory() as d:
        s = NoteStore(os.path.join(d, "n.jsonl"))
        nid = s.add("first", "hello world")
        assert nid == 1
        notes = s.list_notes()
        assert len(notes) == 1
        assert notes[0]["title"] == "first"
        assert notes[0]["body"] == "hello world"


def test_ids_increment():
    with tempfile.TemporaryDirectory() as d:
        s = NoteStore(os.path.join(d, "n.jsonl"))
        assert s.add("a", "1") == 1
        assert s.add("b", "2") == 2


def test_find():
    with tempfile.TemporaryDirectory() as d:
        s = NoteStore(os.path.join(d, "n.jsonl"))
        s.add("groceries", "milk and eggs")
        s.add("gym", "leg day")
        hits = s.find("milk")
        assert [n["title"] for n in hits] == ["groceries"]
        assert s.find("zzz") == []


if __name__ == "__main__":
    test_add_and_list()
    test_ids_increment()
    test_find()
    print("seed tests ok")
'''


def setup(workdir):
    pkg = os.path.join(workdir, "notes")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write("from .store import NoteStore\n")
    with open(os.path.join(pkg, "store.py"), "w") as f:
        f.write(SEED_STORE)
    with open(os.path.join(pkg, "cli.py"), "w") as f:
        f.write(SEED_CLI)
    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write(SEED_README)
    tests = os.path.join(workdir, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(tests, "test_seed.py"), "w") as f:
        f.write(SEED_TEST)
