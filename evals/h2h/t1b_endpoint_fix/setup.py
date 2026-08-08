import os

# Seed project: a stdlib-only API client package, pre-turn-1.
# It ships the Transport contract (the seam every test fake plugs into) and a
# bare APIClient shell. The 7 user turns build the client out incrementally.
# NOTE: the seed deliberately contains NO endpoint paths anywhere on disk —
# endpoint knowledge arrives only through conversation.

SEED_TRANSPORT = '''\
"""Transport layer for the API client.

Design notes (READ THIS before changing behavior):
  * A Transport has ONE job:
        request(method, path, headers=None, params=None, body=None)
            -> (status, headers, body_bytes)
    - ``status`` is an int HTTP status code.
    - ``headers`` is a plain dict of response headers.
    - ``body_bytes`` is the raw response body as ``bytes`` (b"" if empty).
  * Do NOT change this signature; tests drive the client through fake
    transports that implement exactly this contract.
  * Callers pass the bare ``path``; joining it onto a base URL and encoding
    ``params`` into the query string is the transport's job.
"""
import urllib.parse
import urllib.request


class Transport:
    """Abstract transport. Subclass and implement request()."""

    def request(self, method, path, headers=None, params=None, body=None):
        raise NotImplementedError


class UrllibTransport(Transport):
    """Real HTTP transport over urllib (stdlib only)."""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def request(self, method, path, headers=None, params=None, body=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method, data=body)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req) as resp:
            return (resp.status, dict(resp.headers), resp.read())
'''

SEED_API = '''\
"""High-level API client. The seed ships only the shell; features land
incrementally. Build on the Transport contract in transport.py."""
from .transport import Transport


class ApiError(Exception):
    """Raised when the server answers with an error status."""

    def __init__(self, status, message=""):
        super().__init__(message or "API error %s" % status)
        self.status = status


class APIClient:
    def __init__(self, transport):
        self.transport = transport
'''

SEED_README = '''\
# apiclient

A dependency-free (stdlib-only) HTTP client for the internal service.

Layout:
  * `client/transport.py` — the Transport contract (+ a urllib implementation)
  * `client/api.py` — the high-level APIClient (being built out)

Rules: keep the `Transport.request` signature stable; test fakes rely on it.
'''

SEED_TEST = '''\
"""Seed smoke tests. Keep these green."""
from client.api import APIClient, ApiError
from client.transport import Transport


class NullTransport(Transport):
    def request(self, method, path, headers=None, params=None, body=None):
        return (200, {}, b"")


def test_construct():
    c = APIClient(NullTransport())
    assert c.transport is not None


def test_apierror_carries_status():
    e = ApiError(503, "boom")
    assert e.status == 503
    assert "boom" in str(e)


if __name__ == "__main__":
    test_construct()
    test_apierror_carries_status()
    print("seed tests ok")
'''


def setup(workdir):
    pkg = os.path.join(workdir, "client")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write("from .api import APIClient, ApiError\n")
    with open(os.path.join(pkg, "transport.py"), "w") as f:
        f.write(SEED_TRANSPORT)
    with open(os.path.join(pkg, "api.py"), "w") as f:
        f.write(SEED_API)
    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write(SEED_README)
    tests = os.path.join(workdir, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(tests, "test_seed.py"), "w") as f:
        f.write(SEED_TEST)
