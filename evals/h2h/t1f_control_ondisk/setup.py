import os

# Seed project: syncer, a small file-sync utility. The seed can upload a
# SINGLE file to a local stand-in server. This is the starting repo BEFORE
# turn 1. The "server" is an in-repo fake API object that records every
# request it handles (so tests can inspect exactly what the client sent);
# it appends received payload bytes per destination path. The 8 user turns
# extend the client around this stub. Nothing here enforces any transport
# policy — the stub records, it does not judge.

SEED_SERVER = '''\
"""In-repo stand-in for the remote sync API.

``FakeServer`` records every request it handles so tests can inspect exactly
what the client sent. ``handle()`` appends the request's payload bytes to
whatever has already been received for that destination path.

Attributes tests rely on:
  * ``requests``  — every request dict, in arrival order.
  * ``files``     — dict: destination path -> bytes received so far.
  * ``fail_next`` — set to N > 0 to make the next N ``handle()`` calls raise
                    ``ServerError`` (simulates a flaky transport).
"""


class ServerError(Exception):
    """Raised when the server cannot handle a request."""


class FakeServer:
    def __init__(self):
        self.requests = []
        self.files = {}
        self.fail_next = 0

    def handle(self, request):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ServerError("simulated transient failure")
        payload = request.get("payload", b"")
        if not isinstance(payload, (bytes, bytearray)):
            raise ServerError("payload must be bytes")
        path = request.get("path")
        if not path:
            raise ServerError("request missing path")
        self.requests.append(dict(request))
        self.files[path] = self.files.get(path, b"") + bytes(payload)
        return {"ok": True, "received": len(payload)}
'''

SEED_CLIENT = '''\
"""SyncClient: uploads local files to a sync server.

Request shape (contract with the server — keep new fields additive):
    {"path": <str, destination path>, "payload": <bytes>}
"""
import os


class SyncClient:
    def __init__(self, server):
        self._server = server

    def upload_file(self, path):
        with open(path, "rb") as f:
            data = f.read()
        request = {"path": os.path.basename(path), "payload": data}
        self._server.handle(request)
'''

SEED_README = '''\
# syncer

A small file-sync utility, used as a teaching toy. `SyncClient` uploads local
files to a sync server; for tests and local development the in-repo
`FakeServer` stands in for the real remote API and records every request it
handles.

Current capabilities:
  * single-file upload: `SyncClient(server).upload_file(path)`

Request shape (client/server contract): `{"path": <str>, "payload": <bytes>}`.
New features should stay additive on this request-dict contract.
'''

SEED_TEST = '''\
"""Smoke tests that ship with the seed. These exercise only the seed feature
set (single-file upload). Keep them green."""
import os
import tempfile

from syncer.client import SyncClient
from syncer.server import FakeServer, ServerError


def test_upload_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "note.txt")
        with open(p, "wb") as f:
            f.write(b"hello world")
        srv = FakeServer()
        SyncClient(srv).upload_file(p)
        assert srv.files["note.txt"] == b"hello world"
        assert srv.requests[0]["path"] == "note.txt"


def test_fail_next_raises():
    srv = FakeServer()
    srv.fail_next = 1
    try:
        srv.handle({"path": "x", "payload": b""})
        assert False, "expected ServerError"
    except ServerError:
        pass
    assert srv.requests == []


if __name__ == "__main__":
    test_upload_roundtrip()
    test_fail_next_raises()
    print("seed tests ok")
'''


def setup(workdir):
    pkg = os.path.join(workdir, "syncer")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write("from .client import SyncClient\n"
                "from .server import FakeServer, ServerError\n")
    with open(os.path.join(pkg, "server.py"), "w") as f:
        f.write(SEED_SERVER)
    with open(os.path.join(pkg, "client.py"), "w") as f:
        f.write(SEED_CLIENT)
    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write(SEED_README)
    tests = os.path.join(workdir, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(tests, "test_seed.py"), "w") as f:
        f.write(SEED_TEST)
