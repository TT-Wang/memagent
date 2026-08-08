import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def setup(workdir):
    pkg = "httpretry"

    _w(workdir, os.path.join(pkg, "__init__.py"), '''\
"""httpretry: a tiny HTTP retry-decision toolkit.

The library decides, for a failed HTTP response, whether the request should
be retried, how long to wait before retrying, and whether a server-supplied
``Retry-After`` header should be honored.

The central CONTRACT is the *retry classification* of a status code, defined
in ``status.py`` (``RetryClass`` + ``classify``). Every other module
(``policy``, ``backoff``, ``retryafter``, ``client``) must agree with that
classification. Re-exported here for convenience.
"""
from .status import RetryClass, classify
from .policy import RetryPolicy
from .backoff import delay_for
from .retryafter import honors_retry_after
from .client import RetryClient

__all__ = [
    "RetryClass",
    "classify",
    "RetryPolicy",
    "delay_for",
    "honors_retry_after",
    "RetryClient",
]
''')

    # -----------------------------------------------------------------------
    # status.py -- the SINGLE SOURCE OF TRUTH for how a status code is
    # classified. BUG: the rate-limited codes (429, 413) are NOT mapped to
    # RATE_LIMITED here; 429 falls through to PERMANENT and 413 is absent.
    # -----------------------------------------------------------------------
    _w(workdir, os.path.join(pkg, "status.py"), '''\
"""Retry classification of HTTP status codes -- the shared contract.

``RetryClass`` is the vocabulary every other module speaks. ``classify(code)``
is the single source of truth that maps an HTTP status code to its class.

Three classes exist:

* ``TRANSIENT``    -- a generic server-side hiccup (500, 502, 503, 504).
                      Safe to retry with a normal backoff.
* ``RATE_LIMITED`` -- the server is throttling us (429 Too Many Requests,
                      413 Payload Too Large used as a soft throttle here).
                      Should be retried, but more gently, and any
                      ``Retry-After`` header it sends must be honored.
* ``PERMANENT``    -- not worth retrying (400, 401, 403, 404, ...).

NOTE: ``RATE_LIMITED`` was added to ``RetryClass`` recently, but the
classification table below has not been fully wired up for it yet.
"""
from enum import Enum


class RetryClass(Enum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    PERMANENT = "permanent"


# Explicit per-code classification. Anything not listed is PERMANENT.
_TRANSIENT_CODES = frozenset([500, 502, 503, 504])

# BUG: the rate-limited codes are not classified here. 429/413 therefore fall
# through to PERMANENT below, which contradicts the RATE_LIMITED class that the
# rest of the package now expects.
_RATE_LIMITED_CODES = frozenset([])


def classify(code):
    """Return the RetryClass for an HTTP status code."""
    if code in _TRANSIENT_CODES:
        return RetryClass.TRANSIENT
    if code in _RATE_LIMITED_CODES:
        return RetryClass.RATE_LIMITED
    return RetryClass.PERMANENT
''')

    # -----------------------------------------------------------------------
    # policy.py -- decides whether to retry at all. BUG: only TRANSIENT is
    # treated as retryable; RATE_LIMITED is dropped.
    # -----------------------------------------------------------------------
    _w(workdir, os.path.join(pkg, "policy.py"), '''\
"""Decides WHETHER a failed response should be retried.

The decision is driven by the shared classification in ``status.classify``.
"""
from .status import RetryClass, classify


class RetryPolicy:
    """Holds retry limits and answers ``is_retryable(code)``."""

    def __init__(self, max_attempts=3):
        self.max_attempts = max_attempts

    def is_retryable(self, code):
        """Return True if a response with this status code should be retried.

        BUG: only TRANSIENT responses are retried here. RATE_LIMITED responses
        (429, 413) are silently treated as non-retryable, even though the rest
        of the package is built to retry them gently.
        """
        return classify(code) is RetryClass.TRANSIENT

    def should_attempt(self, attempt):
        """Return True if another attempt is allowed given attempts so far."""
        return attempt < self.max_attempts
''')

    # -----------------------------------------------------------------------
    # backoff.py -- computes the wait before the next attempt. BUG: the
    # per-class base-delay table has no RATE_LIMITED entry, so a 429 KeyErrors.
    # -----------------------------------------------------------------------
    _w(workdir, os.path.join(pkg, "backoff.py"), '''\
"""Computes how long to wait before the next retry attempt.

The base delay depends on the response's RetryClass: rate-limited responses
should back off more aggressively than generic transient ones.
"""
from .status import RetryClass, classify

# Base delay (seconds) per retry class. Multiplied by 2 ** (attempt - 1).
# BUG: there is no RATE_LIMITED entry, so delay_for() raises KeyError for a
# 429/413 response once those are classified as RATE_LIMITED.
_BASE_DELAY = {
    RetryClass.TRANSIENT: 0.5,
}


def delay_for(code, attempt):
    """Return the backoff delay in seconds for ``code`` on ``attempt`` (1-based).

    PERMANENT responses get 0 (they are never retried).
    """
    cls = classify(code)
    if cls is RetryClass.PERMANENT:
        return 0.0
    base = _BASE_DELAY[cls]
    return base * (2 ** (attempt - 1))
''')

    # -----------------------------------------------------------------------
    # retryafter.py -- whether to honor a server Retry-After header. BUG: the
    # set of codes that honor Retry-After is duplicated here and only has 503.
    # -----------------------------------------------------------------------
    _w(workdir, os.path.join(pkg, "retryafter.py"), '''\
"""Decides whether a server-supplied ``Retry-After`` header must be honored.

Per the contract, RATE_LIMITED responses (and 503) carry an authoritative
``Retry-After`` that overrides our computed backoff.
"""
from .status import RetryClass, classify

# BUG: this is a hand-maintained duplicate of "which codes honor Retry-After".
# It has drifted: it only lists 503 and never consults the RATE_LIMITED class,
# so 429/413 Retry-After headers are ignored.
_RETRY_AFTER_CODES = frozenset([503])


def honors_retry_after(code):
    """Return True if a Retry-After header for this code must be honored."""
    return code in _RETRY_AFTER_CODES
''')

    # -----------------------------------------------------------------------
    # client.py -- the orchestrator that ties policy + backoff + retryafter
    # together. It is correct in structure; it breaks only because its
    # collaborators disagree about RATE_LIMITED.
    # -----------------------------------------------------------------------
    _w(workdir, os.path.join(pkg, "client.py"), '''\
"""High-level retry orchestrator.

``RetryClient.next_wait(code, attempt, headers)`` returns the number of
seconds to wait before the next attempt, or ``None`` if the request should
NOT be retried. It composes the three lower-level decisions:

  1. policy.is_retryable(code)      -- retry at all?
  2. retryafter.honors_retry_after  -- does the server override our backoff?
  3. backoff.delay_for(code, attempt) -- otherwise compute our own backoff.

This module is structurally correct; it only misbehaves when its collaborators
disagree about how a status code is classified.
"""
from .policy import RetryPolicy
from .backoff import delay_for
from .retryafter import honors_retry_after


class RetryClient:
    def __init__(self, policy=None):
        self.policy = policy or RetryPolicy()

    def next_wait(self, code, attempt, headers=None):
        """Return seconds to wait before the next attempt, or None to stop."""
        headers = headers or {}
        if not self.policy.is_retryable(code):
            return None
        if not self.policy.should_attempt(attempt):
            return None
        if honors_retry_after(code) and "Retry-After" in headers:
            return float(headers["Retry-After"])
        return delay_for(code, attempt)
''')

    # -----------------------------------------------------------------------
    # DISTRACTOR: statuscodes.py -- shares the "status" substring and literally
    # contains 429/413 and the word "retry" in prose, but it is a pure
    # reason-phrase lookup table. It MUST stay byte-identical; a blanket
    # find-replace that "adds 429 handling" here is wrong.
    # -----------------------------------------------------------------------
    _w(workdir, os.path.join(pkg, "statuscodes.py"), '''\
"""HTTP status code reason phrases. DISTRACTOR -- do NOT change for this fix.

This is a passive lookup table mapping numeric codes to their human-readable
reason phrase. It deliberately mentions 429 and 413 and the word "retry" in
prose, but it has nothing to do with the retry-classification contract. Editing
this file (e.g. via a blanket find-replace of 429/413) is incorrect.
"""

# code -> reason phrase. (RFC 9110 reason phrases.)
REASON_PHRASES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    408: "Request Timeout",
    413: "Content Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


def name_for(code):
    """Return the reason phrase for a status code (or 'Unknown')."""
    return REASON_PHRASES.get(code, "Unknown")


def is_client_error(code):
    """True for 4xx codes. Note: a 429 may still be worth a retry elsewhere."""
    return 400 <= code < 500
''')

    # -----------------------------------------------------------------------
    # DISTRACTOR: version.py -- unrelated metadata, must NOT change.
    # -----------------------------------------------------------------------
    _w(workdir, os.path.join(pkg, "version.py"), '''\
"""Version metadata. DISTRACTOR -- do NOT change for this fix."""

__version__ = "0.3.0"


def version_tuple():
    return tuple(int(p) for p in __version__.split("."))
''')

    _w(workdir, "README.md", '''\
# httpretry

A tiny HTTP retry-decision toolkit.

```python
from httpretry import RetryClient

client = RetryClient()
# 503 with a Retry-After header -> honor the header
client.next_wait(503, attempt=1, headers={"Retry-After": "7"})  # -> 7.0
# 500 transient -> computed backoff
client.next_wait(500, attempt=1)  # -> 0.5
# 404 permanent -> do not retry
client.next_wait(404, attempt=1)  # -> None
```

The retry *classification* of a status code (`status.RetryClass` /
`status.classify`) is the single source of truth. `policy`, `backoff` and
`retryafter` must all agree with it.
''')
