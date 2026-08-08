import os


# ---------------------------------------------------------------------------
# This scenario builds a small, coherent "DSV" (delimiter-separated values)
# record codec -- a self-contained CSV-like library with TWO features that
# share one quoting convention:
#
#   * Feature Y (ENCODING / serialization): dsv.encode_record(fields) -> line.
#     It is CORRECT and ships with passing tests. A field is quoted with double
#     quotes when it contains the delimiter, a quote, or a newline; an embedded
#     quote is escaped by DOUBLING it ("" inside a quoted field) -- the standard
#     CSV convention.
#
#   * Feature X (DECODING / parsing): dsv.decode_record(line) -> fields. It is
#     BUGGY: its quoted-field scanner does not handle the doubled-quote escape.
#     When it sees the first quote of an escaped pair ("") inside a quoted field
#     it treats it as the CLOSING quote and stops, so any field that contains an
#     embedded quote is split / truncated. Plain fields and fields that merely
#     contain the delimiter decode fine, which is why the symptom is easy to miss
#     unless you feed it a value with a quote in it.
#
# The two features share the SAME escaping convention and the SAME module, so a
# careless fix to the decoder (e.g. changing the escape char, rewriting the
# shared quoting rules, or "simplifying" the encoder to match a wrong decoder)
# silently breaks ENCODING round-trip. The task is to fix decoding WITHOUT
# regressing encoding -- a targeted change to the decoder's quoted-field state
# machine.
#
# The bug is planted by replacing a single, unique block in the decoder so it is
# deterministic and the reference fix reverts exactly that block.
# ---------------------------------------------------------------------------


# The CORRECT decoder inner-loop block (handles the doubled-quote escape) and
# the planted buggy block (no escape handling: first quote ends the field). The
# reference fix swaps _BUG back to _GOOD.
_GOOD = '''\
            if c == quote:
                # A doubled quote ("") inside a quoted field is a single
                # literal quote; peek ahead to tell an escape from the close.
                if i + 1 < n and line[i + 1] == quote:
                    buf.append(quote)
                    i += 2
                    continue
                # A lone quote closes the quoted run.
                in_quotes = False
                i += 1
                continue
'''

_BUG = '''\
            if c == quote:
                # A lone quote closes the quoted run.
                in_quotes = False
                i += 1
                continue
'''


_DSV = '''\
"""dsv: a tiny CSV-like record codec (one record == one line).

A record is a list of string fields. ``encode_record`` serializes a list of
fields into a single delimited line; ``decode_record`` parses such a line back
into a list of fields. The two are inverses: ``decode_record(encode_record(x))``
must equal ``x`` for every list of strings ``x``.

Quoting convention (shared by both directions):

  * The field delimiter is ``,`` and records are newline-terminated.
  * A field is wrapped in double quotes when it contains the delimiter, a double
    quote, a carriage return, or a newline; otherwise it is emitted bare.
  * Inside a quoted field an embedded double quote is escaped by DOUBLING it
    (``"`` -> ``""``) -- the standard CSV rule.

Empty records and empty fields are representable: an empty field is the empty
string, and a single empty field encodes to an empty line.
"""
from __future__ import annotations

DELIM = ","
QUOTE = '"'
_MUST_QUOTE = {DELIM, QUOTE, "\\n", "\\r"}


def _needs_quoting(field):
    """True when ``field`` cannot be emitted bare and must be quoted."""
    if field == "":
        return False
    return any(ch in field for ch in _MUST_QUOTE)


def encode_field(field):
    """Serialize a single field, quoting + escaping only if required."""
    if not _needs_quoting(field):
        return field
    escaped = field.replace(QUOTE, QUOTE + QUOTE)
    return QUOTE + escaped + QUOTE


def encode_record(fields):
    """Serialize a list of string fields into one delimited line.

    The returned line does NOT include the trailing newline; callers add the
    line separator. ``encode_record([])`` is the empty string.
    """
    if not isinstance(fields, (list, tuple)):
        raise TypeError("fields must be a list or tuple of strings")
    return DELIM.join(encode_field(f) for f in fields)


def decode_record(line, delim=DELIM, quote=QUOTE):
    """Parse a delimited line back into a list of string fields.

    Handles bare fields, quoted fields, embedded delimiters inside quotes, and
    embedded quotes escaped by doubling. The inverse of ``encode_record``.
    """
    # Strip a single trailing newline (\\r\\n or \\n) if present; interior
    # newlines only occur inside quoted fields and are preserved.
    if line.endswith("\\r\\n"):
        line = line[:-2]
    elif line.endswith("\\n"):
        line = line[:-1]

    fields = []
    buf = []
    in_quotes = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if in_quotes:
            if c == quote:
                # A doubled quote ("") inside a quoted field is a single
                # literal quote; peek ahead to tell an escape from the close.
                if i + 1 < n and line[i + 1] == quote:
                    buf.append(quote)
                    i += 2
                    continue
                # A lone quote closes the quoted run.
                in_quotes = False
                i += 1
                continue
            buf.append(c)
            i += 1
            continue
        # outside quotes
        if c == quote:
            in_quotes = True
            i += 1
            continue
        if c == delim:
            fields.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1

    fields.append("".join(buf))
    return fields
'''


_TABLE = '''\
"""table: read/write a list of records as DSV lines, built on the dsv codec.

A ``Table`` is just a list of records (each record a list of string fields).
``dumps`` serializes the whole table to text and ``loads`` parses it back, so
``loads(dumps(t)) == t``. This module is the thin layer the app/tests use; the
quoting logic all lives in dsv.
"""
from __future__ import annotations

from dsv import encode_record, decode_record


def dumps(rows):
    """Serialize a list of records into newline-terminated DSV text."""
    return "".join(encode_record(r) + "\\n" for r in rows)


def loads(text):
    """Parse newline-terminated DSV text back into a list of records.

    A trailing newline does not produce a spurious empty record.
    """
    if text == "":
        return []
    lines = text.split("\\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [decode_record(line) for line in lines]
'''


_TEST = '''\
"""Test suite for the dsv record codec.

Run with:  python -m unittest -v test_dsv

The ENCODING tests (TestEncode, plus the encode half of TestTable) pass: the
serializer is correct. The DECODING side currently MISHANDLES fields that
contain an embedded double quote -- decode_record stops at the first quote of
an escaped "" pair, so round-trip breaks for any value with a quote in it.
Make decoding correct (round-trip must hold) WITHOUT regressing encoding.
"""
import unittest

import dsv
import table


class TestEncode(unittest.TestCase):
    def test_plain_fields(self):
        self.assertEqual(dsv.encode_record(["a", "b", "c"]), "a,b,c")

    def test_empty_and_single(self):
        self.assertEqual(dsv.encode_record([]), "")
        self.assertEqual(dsv.encode_record([""]), "")
        self.assertEqual(dsv.encode_record(["", ""]), ",")

    def test_quotes_delimiter(self):
        # A field with the delimiter gets wrapped in quotes.
        self.assertEqual(dsv.encode_record(["a,b", "c"]), '"a,b",c')

    def test_escapes_embedded_quote(self):
        # An embedded quote is doubled and the field is wrapped.
        self.assertEqual(dsv.encode_record(['he said "hi"']),
                         '"he said ""hi"""')
        self.assertEqual(dsv.encode_record(['"']), '""""')


class TestTableEncode(unittest.TestCase):
    def test_dumps_plain(self):
        self.assertEqual(table.dumps([["a", "b"], ["c", "d"]]),
                         "a,b\\nc,d\\n")

    def test_dumps_empty(self):
        self.assertEqual(table.dumps([]), "")


if __name__ == "__main__":
    unittest.main()
'''


_README = '''\
dsv -- tiny CSV-like record codec
=================================

``dsv.py`` encodes/decodes records (lists of string fields) to/from single
delimited lines. ``table.py`` is a thin layer that reads/writes whole tables of
records. The two directions share one quoting convention and are meant to be
exact inverses: ``decode_record(encode_record(x)) == x``.

Quoting convention
------------------
* delimiter is ``,``; records are newline-terminated.
* a field is wrapped in double quotes if it contains the delimiter, a quote, a
  carriage return, or a newline.
* inside a quoted field an embedded quote is escaped by DOUBLING it (`"` -> `""`).

What works
----------
ENCODING is correct, including escaping embedded quotes::

    >>> import dsv
    >>> dsv.encode_record(['he said "hi"', 'plain'])
    '"he said ""hi""",plain'

Observed problem
----------------
DECODING does not round-trip when a field contains an embedded quote. The
decoder treats the FIRST quote of an escaped ``""`` pair as the end of the
field, so the value is truncated / mis-split::

    >>> dsv.decode_record('"he said ""hi""",plain')
    ['he said ', 'hi', '', 'plain']     # WRONG
    # expected: ['he said "hi"', 'plain']

    >>> import table
    >>> t = [['a"b', 'c'], ['d', 'e']]
    >>> table.loads(table.dumps(t)) == t
    False                                # round-trip is broken

Plain fields and fields that merely contain the delimiter decode fine; only
values with an embedded quote are affected. The encoder is correct, so the
defect is in the DECODER's quoted-field scanning, inside ``dsv.py``. Fix the
decoder so round-trip holds again, and do not break encoding to do it.
'''


def setup(workdir):
    """Build the dsv record-codec library + tests in ``workdir`` and plant the
    decoder escaped-quote bug deterministically."""
    os.makedirs(workdir, exist_ok=True)

    # Assemble the CORRECT dsv module, then corrupt exactly the escaped-quote
    # block in the decoder so the first quote of a "" pair ends the field.
    good_dsv = _DSV
    if good_dsv.count(_GOOD) != 1:
        raise RuntimeError("dsv decoder escape block is not unique; bad template")
    buggy_dsv = good_dsv.replace(_GOOD, _BUG, 1)
    if buggy_dsv == good_dsv or _BUG not in buggy_dsv:
        raise RuntimeError("failed to plant dsv decoder bug")

    files = {
        "dsv.py": buggy_dsv,
        "table.py": _TABLE,
        "test_dsv.py": _TEST,
        "README.md": _README,
    }
    for name, body in files.items():
        with open(os.path.join(workdir, name), "w") as f:
            f.write(body)
