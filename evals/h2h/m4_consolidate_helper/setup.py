import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def setup(workdir):
    pkg = "mailkit"

    _w(workdir, os.path.join(pkg, "__init__.py"), '''\
"""mailkit: a tiny self-contained subset of Django's mail/url utilities.

Public surface re-exported for convenience.
"""
from .message import EmailMessage, sanitize_address
from .utils import CachedDnsName, DNS_NAME
from .validators import URLValidator, EmailValidator, ValidationError
from .html import urlize

__all__ = [
    "EmailMessage",
    "sanitize_address",
    "CachedDnsName",
    "DNS_NAME",
    "URLValidator",
    "EmailValidator",
    "ValidationError",
    "urlize",
]
''')

    # ----------------------------------------------------------------------
    # encoding.py: helpers. NOTE: there is NO punycode() helper yet -- the
    # IDN->ASCII idiom is copy-pasted inline across the caller modules. The
    # refactor must add punycode() HERE and route every caller through it.
    # ----------------------------------------------------------------------
    _w(workdir, os.path.join(pkg, "encoding.py"), '''\
"""Encoding helpers (subset of django.utils.encoding).

This is the natural home for a shared IDN->ASCII (Punycode) helper, but one
does not exist yet: the conversion idiom `domain.encode('idna').decode('ascii')`
is currently duplicated inline in validators.py, message.py and html.py, and is
MISSING entirely from utils.py (the bug).
"""


def force_str(s, encoding="utf-8", errors="strict"):
    """Force a bytes/str value to str."""
    if isinstance(s, str):
        return s
    if isinstance(s, bytes):
        return s.decode(encoding, errors)
    return str(s)


def escape_uri_path(path):
    """Quote the path portion of a URI (kept minimal for this package)."""
    from urllib.parse import quote
    return quote(path, safe="/:@&+$,-_.!~*'()")
''')

    # ----------------------------------------------------------------------
    # validators.py: URLValidator / EmailValidator. Each inlines the idiom.
    # ----------------------------------------------------------------------
    _w(workdir, os.path.join(pkg, "validators.py"), '''\
"""URL and email validators (subset of django.core.validators)."""
from urllib.parse import urlsplit, urlunsplit


class ValidationError(Exception):
    pass


def url_to_ace(url):
    """Normalize a URL's host to its ASCII-compatible (ACE/Punycode) form.

    Used by URLValidator to accept internationalized domain names.
    """
    scheme, netloc, path, query, fragment = urlsplit(url)
    if netloc:
        try:
            netloc = netloc.encode('idna').decode('ascii')  # IDN -> ACE
        except UnicodeError:  # invalid domain part
            raise ValidationError("invalid domain in URL: %r" % (url,))
        url = urlunsplit((scheme, netloc, path, query, fragment))
    return url


class URLValidator:
    """Very small URL validator: requires scheme + netloc, normalizes IDN."""

    def __call__(self, value):
        scheme = urlsplit(value).scheme
        if scheme not in ("http", "https"):
            raise ValidationError("unsupported scheme: %r" % (value,))
        if not urlsplit(value).netloc:
            raise ValidationError("missing host: %r" % (value,))
        # Normalize the host to ACE form; returns the normalized URL.
        return url_to_ace(value)


class EmailValidator:
    """Very small email validator that normalizes the domain part to ACE."""

    def __call__(self, value):
        if value.count("@") != 1:
            raise ValidationError("invalid email: %r" % (value,))
        local_part, domain_part = value.rsplit("@", 1)
        if not local_part or not domain_part:
            raise ValidationError("invalid email: %r" % (value,))
        # Try for possible IDN domain-part.
        try:
            domain_part = domain_part.encode('idna').decode('ascii')
        except UnicodeError:
            raise ValidationError("invalid domain: %r" % (value,))
        return "%s@%s" % (local_part, domain_part)
''')

    # ----------------------------------------------------------------------
    # utils.py: the BUG site. get_fqdn() returns the raw hostname WITHOUT
    # punycoding it, so a non-ASCII hostname breaks Message-ID encoding.
    # ----------------------------------------------------------------------
    _w(workdir, os.path.join(pkg, "utils.py"), '''\
"""Mail utilities (subset of django.core.mail.utils).

CachedDnsName caches the fully-qualified domain name of the local host and is
exported as DNS_NAME. BUG: get_fqdn() does NOT convert a non-ASCII hostname to
Punycode, so when the host name is non-ASCII the Message-ID header cannot be
encoded under a non-unicode email encoding.
"""
import socket


class CachedDnsName:
    """Cache the hostname lazily; socket.getfqdn() can be slow."""

    def __str__(self):
        return self.get_fqdn()

    def get_fqdn(self):
        if not hasattr(self, "_fqdn"):
            # BUG: raw hostname is used verbatim; non-ASCII names are not
            # converted to their ASCII-compatible (Punycode) form here.
            self._fqdn = socket.getfqdn()
        return self._fqdn


DNS_NAME = CachedDnsName()
''')

    # ----------------------------------------------------------------------
    # message.py: sanitize_address inlines the idiom; EmailMessage builds a
    # Message-ID from DNS_NAME and encodes headers under email.encoding.
    # ----------------------------------------------------------------------
    _w(workdir, os.path.join(pkg, "message.py"), '''\
"""Email message building (subset of django.core.mail.message)."""
import itertools
from email.charset import Charset
from email.header import Header

from .utils import DNS_NAME

_id_counter = itertools.count(1)


def encode_header(value, encoding):
    """Render a header value under `encoding` (subset of Django's
    forbid_multi_line_headers): an ASCII value is kept verbatim, otherwise it is
    encoded via the charset -- which raises UnicodeEncodeError for a non-unicode
    charset that cannot represent the value.
    """
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return Header(value, Charset(encoding)).encode()


def sanitize_address(addr, encoding):
    """Normalize an address's domain to its ACE form for safe header use.

    addr may be "name@domain" or a bare "domain" fragment.
    """
    if "@" in addr:
        localpart, domain = addr.rsplit("@", 1)
    else:
        localpart, domain = "", addr
    try:
        domain.encode("ascii")
    except UnicodeEncodeError:
        domain = domain.encode('idna').decode('ascii')
    if localpart:
        return "%s@%s" % (localpart, domain)
    return domain


def make_message_id(domain):
    """Build a Message-ID value <unique@domain> (domain used verbatim)."""
    unique = "%d.%d" % (next(_id_counter), id(object()))
    return "<%s@%s>" % (unique, domain)


class EmailMessage:
    """Minimal email message; .message() renders headers under self.encoding."""

    def __init__(self, subject, body, from_email, to):
        self.subject = subject
        self.body = body
        self.from_email = from_email
        self.to = list(to)
        self.encoding = None  # None => utf-8; set to e.g. 'iso-8859-1'

    def message(self):
        """Return a dict of rendered headers. Raises if a header cannot be
        encoded under the selected (possibly non-unicode) encoding.
        """
        encoding = self.encoding or "utf-8"
        # Message-ID embeds the local host's domain name.
        msgid = make_message_id(DNS_NAME.get_fqdn())
        headers = {
            "Subject": self.subject,
            "From": self.from_email,
            "To": ", ".join(self.to),
            # A non-ASCII domain here raises UnicodeEncodeError under a
            # non-unicode charset (this is the bug -- the domain was never
            # converted to its ASCII-compatible Punycode form).
            "Message-ID": encode_header(msgid, encoding),
        }
        return headers
''')

    # ----------------------------------------------------------------------
    # html.py: urlize inlines the idiom when linkifying domains/emails.
    # ----------------------------------------------------------------------
    _w(workdir, os.path.join(pkg, "html.py"), '''\
"""HTML helpers (subset of django.utils.html): a tiny urlize()."""


def _ace(domain):
    # (Local conversion used by urlize.) IDN -> ACE.
    return domain.encode('idna').decode('ascii')


def urlize(text):
    """Turn bare domains in `text` into <a href> links, normalizing IDNs.

    Tokens that look like a host (contain a dot, no scheme) are linked; the
    host is converted to its ASCII-compatible (Punycode) form first.
    """
    out = []
    for token in text.split():
        if "://" in token or "@" in token:
            out.append(token)
            continue
        if "." in token and " " not in token:
            try:
                host = _ace(token)
            except UnicodeError:
                out.append(token)
                continue
            out.append('<a href="http://%s">%s</a>' % (host, token))
        else:
            out.append(token)
    return " ".join(out)
''')

    # ---- DISTRACTOR FILES: must NOT change ----
    # http.py contains a same-shaped `.encode('idna')` line, but it is DEAD /
    # unrelated legacy code (commented snippet + an unrelated bytes helper).
    _w(workdir, os.path.join(pkg, "http.py"), '''\
"""Low-level HTTP byte helpers. DO NOT change for this refactor.

The line below looks like the IDN idiom but is unrelated dead code: it is a
commented-out legacy snippet kept for documentation, and to_ascii_bytes() is a
generic byte coercion helper that has nothing to do with domain punycoding.
"""


def to_ascii_bytes(value):
    """Coerce a str/bytes header value to ASCII bytes (raises on non-ASCII)."""
    if isinstance(value, bytes):
        return value
    return value.encode("ascii")


# Legacy reference (DEAD CODE -- intentionally not called anywhere):
#   domain = host.encode('idna').decode('ascii')
def legacy_note():
    """Return documentation about the old inline IDN handling."""
    return "old idiom was: host.encode('idna').decode('ascii')"
''')

    _w(workdir, os.path.join(pkg, "version.py"), '''\
"""Version metadata. DO NOT change for this refactor."""

__version__ = "2.1.0"


def version_tuple():
    return tuple(int(p) for p in __version__.split("."))
''')

    _w(workdir, "README.md", '''\
# mailkit

A tiny self-contained subset of Django's mail / URL utilities.

```python
from mailkit import EmailMessage, URLValidator, urlize

email = EmailMessage("subject", "body", "from@example.com", ["to@example.com"])
email.encoding = "iso-8859-1"
email.message()  # builds headers, including a Message-ID from the local host
```

The IDN -> ASCII (Punycode) conversion `domain.encode('idna').decode('ascii')`
is currently duplicated across `validators.py`, `message.py` and `html.py`, and
is missing from `utils.py`. It should be consolidated into a single helper.
''')
