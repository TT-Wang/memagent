"""Reference solution (VALIDATION ONLY -- never shown to benchmarked agents).

Consolidates the duplicated IDN->ASCII idiom into a single punycode() helper in
encoding.py and rewires every caller (validators.py, message.py, html.py) plus
the actual bug site (utils.py get_fqdn) to use it. Mirrors the real
django__django-11532 gold patch (commits f226bdbf + 55b68de6).
"""
import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    with open(path, "w") as f:
        f.write(content)


def apply(workdir):
    pkg = "mailkit"

    # 1) Add the single shared helper in encoding.py.
    _w(workdir, os.path.join(pkg, "encoding.py"), '''\
"""Encoding helpers (subset of django.utils.encoding).

Home of the single shared IDN->ASCII (Punycode) helper.
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


def punycode(domain):
    """Return the Punycode of the given domain if it's non-ASCII."""
    return domain.encode('idna').decode('ascii')
''')

    # 2) validators.py: route both inline sites through punycode().
    _w(workdir, os.path.join(pkg, "validators.py"), '''\
"""URL and email validators (subset of django.core.validators)."""
from urllib.parse import urlsplit, urlunsplit

from .encoding import punycode


class ValidationError(Exception):
    pass


def url_to_ace(url):
    """Normalize a URL's host to its ASCII-compatible (ACE/Punycode) form.

    Used by URLValidator to accept internationalized domain names.
    """
    scheme, netloc, path, query, fragment = urlsplit(url)
    if netloc:
        try:
            netloc = punycode(netloc)  # IDN -> ACE
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
            domain_part = punycode(domain_part)
        except UnicodeError:
            raise ValidationError("invalid domain: %r" % (value,))
        return "%s@%s" % (local_part, domain_part)
''')

    # 3) utils.py: the bug fix -- punycode the looked-up hostname.
    _w(workdir, os.path.join(pkg, "utils.py"), '''\
"""Mail utilities (subset of django.core.mail.utils)."""
import socket

from .encoding import punycode


class CachedDnsName:
    """Cache the hostname lazily; socket.getfqdn() can be slow."""

    def __str__(self):
        return self.get_fqdn()

    def get_fqdn(self):
        if not hasattr(self, "_fqdn"):
            self._fqdn = punycode(socket.getfqdn())
        return self._fqdn


DNS_NAME = CachedDnsName()
''')

    # 4) message.py: sanitize_address routes through punycode().
    _w(workdir, os.path.join(pkg, "message.py"), '''\
"""Email message building (subset of django.core.mail.message)."""
import itertools
from email.charset import Charset
from email.header import Header

from .encoding import punycode
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
        domain = punycode(domain)
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
            "Message-ID": encode_header(msgid, encoding),
        }
        return headers
''')

    # 5) html.py: urlize routes through punycode().
    _w(workdir, os.path.join(pkg, "html.py"), '''\
"""HTML helpers (subset of django.utils.html): a tiny urlize()."""
from .encoding import punycode


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
                host = punycode(token)
            except UnicodeError:
                out.append(token)
                continue
            out.append('<a href="http://%s">%s</a>' % (host, token))
        else:
            out.append(token)
    return " ".join(out)
''')
