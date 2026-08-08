import os

# Correct, full implementation of syncer after all eight turns.
# VALIDATION ONLY -- never shown to the benchmarked agents.
#
# The buried turn-2 aside made concrete: the remote API rejects any payload
# over 40000 bytes, so upload_file streams the file and sends it as consecutive
# requests whose payloads are each at most 40000 bytes. FakeServer appends
# payloads per destination path, so the file reassembles in arrival order.
# The server stub itself never changed (turn 2 said it shouldn't).

REFERENCE_CLIENT = '''\
"""SyncClient: uploads local files to a sync server.

Request shape (contract with the server — additive on the seed contract):
    {"path": <str, posix-style destination path>,
     "payload": <bytes>,
     "meta": {"size": <int, total file byte length>, "mtime": <int>}}

The real remote API rejects any payload over 40000 bytes, so files are sent as one
or more requests whose payloads are each at most ``_MAX_PAYLOAD`` bytes; the
server appends payloads per path, so multi-request uploads reassemble in
order.
"""
import logging
import os
import time

from .server import ServerError

logger = logging.getLogger("syncer")

_MAX_PAYLOAD = 40000  # remote API rejects any payload larger than 40000 bytes

_RETRIES = 3         # additional attempts after the first (4 total)
_FIRST_DELAY = 0.05  # seconds; doubles per retry: 0.05, 0.1, 0.2


def build_request(path, payload, meta):
    return {"path": path, "payload": payload, "meta": dict(meta)}


class SyncClient:
    def __init__(self, server):
        self._server = server

    def _send(self, request, sleeper):
        delay = _FIRST_DELAY
        attempt = 0
        while True:
            try:
                return self._server.handle(request)
            except ServerError:
                if attempt >= _RETRIES:
                    raise
                sleeper(delay)
                delay *= 2
                attempt += 1

    def upload_file(self, path, root=None, sleeper=time.sleep):
        if root is not None:
            dest = os.path.relpath(path, root).replace(os.sep, "/")
        else:
            dest = os.path.basename(path)
        meta = {"size": os.path.getsize(path),
                "mtime": int(os.path.getmtime(path))}
        total = 0
        with open(path, "rb") as f:
            while True:
                block = f.read(_MAX_PAYLOAD)
                if not block and total > 0:
                    break
                self._send(build_request(dest, block, meta), sleeper)
                total += len(block)
                if len(block) < _MAX_PAYLOAD:
                    break
        logger.info("uploaded %s (%d bytes)", dest, total)
        return {"path": dest, "bytes_sent": total}
'''

REFERENCE_MANIFEST = '''\
"""Manifest building and diffing for directory sync."""
import hashlib
import os


def build_manifest(root):
    manifest = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            h = hashlib.sha256()
            with open(full, "rb") as f:
                h.update(f.read())
            manifest[rel] = h.hexdigest()
    return manifest


def diff_manifests(old, new):
    return {
        "added": sorted(k for k in new if k not in old),
        "removed": sorted(k for k in old if k not in new),
        "changed": sorted(k for k in old if k in new and old[k] != new[k]),
    }
'''

REFERENCE_MAIN = '''\
"""CLI: python -m syncer diff OLD.json NEW.json"""
import json
import sys

from .manifest import diff_manifests


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3 or argv[0] != "diff":
        print("usage: python -m syncer diff OLD.json NEW.json", file=sys.stderr)
        return 2
    with open(argv[1]) as f:
        old = json.load(f)
    with open(argv[2]) as f:
        new = json.load(f)
    d = diff_manifests(old, new)
    for group in ("added", "removed", "changed"):
        for path in d[group]:
            print("%s: %s" % (group, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

REFERENCE_TEST_MANIFEST = '''\
"""Tests for manifest build/diff (turn 3)."""
import hashlib
import os
import tempfile

from syncer.manifest import build_manifest, diff_manifests


def test_build_manifest():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "sub"))
        with open(os.path.join(d, "a.txt"), "wb") as f:
            f.write(b"aa")
        with open(os.path.join(d, "sub", "b.txt"), "wb") as f:
            f.write(b"bb")
        man = build_manifest(d)
        assert set(man) == {"a.txt", "sub/b.txt"}
        assert man["a.txt"] == hashlib.sha256(b"aa").hexdigest()


def test_diff_manifests():
    d = diff_manifests({"a": "1", "b": "2"}, {"b": "3", "c": "4"})
    assert d == {"added": ["c"], "removed": ["a"], "changed": ["b"]}


if __name__ == "__main__":
    test_build_manifest()
    test_diff_manifests()
    print("manifest tests ok")
'''

REFERENCE_TEST_LARGE = '''\
"""Large-file upload test (turn 8)."""
import os
import tempfile

from syncer.client import SyncClient
from syncer.server import FakeServer


def test_large_upload_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "big.bin")
        data = bytes(range(256)) * 1200  # 300 KB
        with open(p, "wb") as f:
            f.write(data)
        srv = FakeServer()
        rec = SyncClient(srv).upload_file(p, sleeper=lambda s: None)
        assert srv.files["big.bin"] == data
        assert rec["bytes_sent"] == len(data)


if __name__ == "__main__":
    test_large_upload_roundtrip()
    print("large-file tests ok")
'''


def apply(workdir):
    pkg = os.path.join(workdir, "syncer")
    with open(os.path.join(pkg, "client.py"), "w") as f:
        f.write(REFERENCE_CLIENT)
    with open(os.path.join(pkg, "manifest.py"), "w") as f:
        f.write(REFERENCE_MANIFEST)
    with open(os.path.join(pkg, "__main__.py"), "w") as f:
        f.write(REFERENCE_MAIN)
    tests = os.path.join(workdir, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(tests, "test_manifest.py"), "w") as f:
        f.write(REFERENCE_TEST_MANIFEST)
    with open(os.path.join(tests, "test_large.py"), "w") as f:
        f.write(REFERENCE_TEST_LARGE)
