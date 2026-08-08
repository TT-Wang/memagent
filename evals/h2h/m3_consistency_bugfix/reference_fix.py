"""Reference solution (VALIDATION ONLY -- never shown to benchmarked agents).

Applies the coordinated multi-file fix so the RATE_LIMITED contract is honored
consistently. Four interdependent files change:

  1. status.py     -- classify 429 and 413 as RATE_LIMITED.
  2. policy.py     -- treat RATE_LIMITED as retryable (not just TRANSIENT).
  3. backoff.py    -- give RATE_LIMITED a base-delay entry.
  4. retryafter.py -- honor Retry-After for RATE_LIMITED codes.

statuscodes.py and version.py are left byte-identical.
"""
import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    with open(path, "w") as f:
        f.write(content)


def apply(workdir):
    pkg = "httpretry"

    # 1) status.py -- map the rate-limited codes to RATE_LIMITED.
    _w(workdir, os.path.join(pkg, "status.py"), '''\
"""Retry classification of HTTP status codes -- the shared contract."""
from enum import Enum


class RetryClass(Enum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    PERMANENT = "permanent"


_TRANSIENT_CODES = frozenset([500, 502, 503, 504])
_RATE_LIMITED_CODES = frozenset([429, 413])


def classify(code):
    """Return the RetryClass for an HTTP status code."""
    if code in _TRANSIENT_CODES:
        return RetryClass.TRANSIENT
    if code in _RATE_LIMITED_CODES:
        return RetryClass.RATE_LIMITED
    return RetryClass.PERMANENT
''')

    # 2) policy.py -- retry both TRANSIENT and RATE_LIMITED.
    _w(workdir, os.path.join(pkg, "policy.py"), '''\
"""Decides WHETHER a failed response should be retried."""
from .status import RetryClass, classify


class RetryPolicy:
    def __init__(self, max_attempts=3):
        self.max_attempts = max_attempts

    def is_retryable(self, code):
        return classify(code) in (RetryClass.TRANSIENT, RetryClass.RATE_LIMITED)

    def should_attempt(self, attempt):
        return attempt < self.max_attempts
''')

    # 3) backoff.py -- add a RATE_LIMITED base delay.
    _w(workdir, os.path.join(pkg, "backoff.py"), '''\
"""Computes how long to wait before the next retry attempt."""
from .status import RetryClass, classify

_BASE_DELAY = {
    RetryClass.TRANSIENT: 0.5,
    RetryClass.RATE_LIMITED: 2.0,
}


def delay_for(code, attempt):
    cls = classify(code)
    if cls is RetryClass.PERMANENT:
        return 0.0
    base = _BASE_DELAY[cls]
    return base * (2 ** (attempt - 1))
''')

    # 4) retryafter.py -- honor Retry-After for RATE_LIMITED (and 503).
    _w(workdir, os.path.join(pkg, "retryafter.py"), '''\
"""Decides whether a server-supplied ``Retry-After`` header must be honored."""
from .status import RetryClass, classify

_TRANSIENT_RETRY_AFTER_CODES = frozenset([503])


def honors_retry_after(code):
    if classify(code) is RetryClass.RATE_LIMITED:
        return True
    return code in _TRANSIENT_RETRY_AFTER_CODES
''')
