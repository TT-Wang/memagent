import os
import sys
import json
import subprocess


# Independent oracle for the regression-safe task: fix DECODING (feature X)
# without breaking ENCODING (feature Y). It does TWO things in FRESH subprocesses
# and never imports the agent's code into this process:
#
#   (1) Runs the WHOLE shipped unittest suite (test_dsv.py) in a fresh
#       `python -B -m unittest` subprocess and requires it ALL green. The suite's
#       encode tests pin feature Y, so this is a first regression guard: the
#       agent must not have weakened encoding (or deleted/edited tests) to make
#       decoding pass.
#
#   (2) Imports the (possibly-edited) dsv.py + table.py in a SEPARATE fresh child
#       and exercises BOTH directions on inputs the agent NEVER SAW:
#         * DECODE-FIX (feature X): decode_record on hand-built encoded strings
#           with embedded quotes returns the right fields.
#         * ENCODE-GUARD (feature Y): encode_record still produces the exact
#           canonical output (quoting + doubled-quote escaping) -- so the agent
#           cannot pass by making encode agree with a still-wrong decode.
#         * ROUND-TRIP: decode_record(encode_record(x)) == x and
#           table.loads(table.dumps(rows)) == rows for unseen values. This is the
#           conjunction that only holds when X is fixed AND Y is preserved.
#       The exact encode outputs are recomputed here in the oracle, independent
#       of the agent's implementation, so app-layer or test-layer hacks cannot
#       satisfy it.


# ---- child #2: both directions on unseen inputs, emitted as JSON -----------
_CODEC_CHILD = r'''
import json, sys
import dsv
import table

out = {"decode": {}, "encode": {}, "roundtrip": {}, "table": {}}

# Unseen DECODE cases: hand-built encoded lines (NOT taken from the test file)
# that the buggy decoder mishandles because of the doubled-quote escape.
_DEC = {
    # one field, embedded quote in the middle
    "d_mid":   '"a""b"',
    # embedded quote AND a delimiter inside the same quoted field
    "d_mixed": '"x"",""y",z',
    # field that is only a quote
    "d_just":  '""""',
    # two quoted fields, each with an embedded quote
    "d_two":   '"p""q","r""s"',
    # quoted field whose embedded quote is at the very end before close
    "d_trail": '"end"""',
    # plain + delimiter-only-quoting field (must stay correct - no escape)
    "d_plain": 'a,"b,c",d',
}
for tag, line in _DEC.items():
    try:
        out["decode"][tag] = {"ok": True, "fields": list(dsv.decode_record(line))}
    except BaseException as e:
        out["decode"][tag] = {"ok": False, "err": "%s:%s" % (type(e).__name__, e)}

# Unseen ENCODE cases (feature Y guard): encoder must still emit canonical form.
_ENC = {
    "e_quote_mid":  ['a"b'],
    "e_quote_delim":['x"y', 'z,w'],
    "e_only_quote": ['"'],
    "e_plain":      ['p', 'q', 'r'],
    "e_empty2":     ['', ''],
    "e_newline":    ['a\nb'],
}
for tag, fields in _ENC.items():
    try:
        out["encode"][tag] = {"ok": True, "line": dsv.encode_record(fields)}
    except BaseException as e:
        out["encode"][tag] = {"ok": False, "err": "%s:%s" % (type(e).__name__, e)}

# Unseen ROUND-TRIP cases (the conjunction): decode(encode(x)) == x.
_RT = {
    "rt_quotes":   ['he said "hi"', 'plain', 'a,b'],
    "rt_all":      ['a"b', 'c,d', 'e"f"g', '', 'h'],
    "rt_nl":       ['line1\nline2', 'tab\there', 'q"q"q'],
    "rt_single_q": ['"'],
    "rt_empty":    [''],
    "rt_delims":   [',,,', '","', 'normal'],
}
for tag, x in _RT.items():
    try:
        enc = dsv.encode_record(x)
        dec = list(dsv.decode_record(enc))
        out["roundtrip"][tag] = {"ok": True, "enc": enc, "dec": dec}
    except BaseException as e:
        out["roundtrip"][tag] = {"ok": False, "err": "%s:%s" % (type(e).__name__, e)}

# Unseen TABLE round-trip: table.loads(table.dumps(rows)) == rows.
_ROWS = [
    ['a"b', 'c'],
    ['d', 'e,f'],
    ['quote"in"here', 'plain'],
    ['', ''],
]
try:
    txt = table.dumps(_ROWS)
    back = table.loads(txt)
    out["table"]["rows"] = {"ok": True, "back": [list(r) for r in back]}
except BaseException as e:
    out["table"]["rows"] = {"ok": False, "err": "%s:%s" % (type(e).__name__, e)}

sys.stdout.write(json.dumps(out))
'''


# ---- oracle-side reference encoder (independent of the agent's dsv.py) ------
_DELIM = ","
_QUOTE = '"'
_MUST_QUOTE = {_DELIM, _QUOTE, "\n", "\r"}


def _ref_encode_field(field):
    if field == "" or not any(ch in field for ch in _MUST_QUOTE):
        return field
    return _QUOTE + field.replace(_QUOTE, _QUOTE + _QUOTE) + _QUOTE


def _ref_encode_record(fields):
    return _DELIM.join(_ref_encode_field(f) for f in fields)


# Expected DECODE results, derived from the encoded-line inputs above.
_EXPECT_DECODE = {
    "d_mid":   ['a"b'],
    "d_mixed": ['x","y', 'z'],
    "d_just":  ['"'],
    "d_two":   ['p"q', 'r"s'],
    "d_trail": ['end"'],
    "d_plain": ['a', 'b,c', 'd'],
}

# Encode inputs (mirror of the child) so the oracle can recompute expected lines.
_ENC_INPUTS = {
    "e_quote_mid":  ['a"b'],
    "e_quote_delim":['x"y', 'z,w'],
    "e_only_quote": ['"'],
    "e_plain":      ['p', 'q', 'r'],
    "e_empty2":     ['', ''],
    "e_newline":    ['a\nb'],
}

# Round-trip inputs (mirror of the child) so the oracle knows what x should be.
_RT_INPUTS = {
    "rt_quotes":   ['he said "hi"', 'plain', 'a,b'],
    "rt_all":      ['a"b', 'c,d', 'e"f"g', '', 'h'],
    "rt_nl":       ['line1\nline2', 'tab\there', 'q"q"q'],
    "rt_single_q": ['"'],
    "rt_empty":    [''],
    "rt_delims":   [',,,', '","', 'normal'],
}

_TABLE_ROWS = [
    ['a"b', 'c'],
    ['d', 'e,f'],
    ['quote"in"here', 'plain'],
    ['', ''],
]


def _drop_pyc(workdir):
    """Drop cached bytecode so we always test the CURRENT source (an edit landing
    in the same wall-clock second can otherwise leave a stale .pyc)."""
    pycache = os.path.join(workdir, "__pycache__")
    if os.path.isdir(pycache):
        for fn in os.listdir(pycache):
            if fn.endswith(".pyc"):
                try:
                    os.remove(os.path.join(pycache, fn))
                except OSError:
                    pass


def verify(workdir):
    for required in ("dsv.py", "table.py", "test_dsv.py"):
        if not os.path.isfile(os.path.join(workdir, required)):
            return False, "%s not found in workdir" % required

    _drop_pyc(workdir)

    # --- (1) WHOLE unittest suite, fresh subprocess, must be all green --------
    try:
        suite = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "-v", "test_dsv"],
            cwd=workdir, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "unittest suite timed out (possible infinite loop)"
    except BaseException as e:
        return False, "could not launch unittest suite: %s" % e

    combined = (suite.stderr or "") + (suite.stdout or "")
    if suite.returncode != 0:
        return False, "unittest suite is NOT all green:\n" + combined[-900:]
    if "OK" not in combined:
        return False, "unittest suite did not report OK:\n" + combined[-900:]
    # The encode tests pin feature Y; they must still be present (not deleted).
    for must in ("test_escapes_embedded_quote", "test_quotes_delimiter"):
        if must not in combined:
            return False, ("encode regression test %s is missing from the run "
                           "-- feature Y tests must not be deleted:\n"
                           % must + combined[-700:])

    # --- (2) BOTH directions on UNSEEN inputs, separate fresh subprocess ------
    _drop_pyc(workdir)
    try:
        child = subprocess.run(
            [sys.executable, "-B", "-c", _CODEC_CHILD],
            cwd=workdir, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "codec oracle child timed out"
    except BaseException as e:
        return False, "could not launch codec oracle: %s" % e

    if child.returncode != 0:
        return False, "codec oracle child crashed (rc=%d): %s" % (
            child.returncode, (child.stderr or child.stdout)[-600:])
    try:
        res = json.loads(child.stdout)
    except Exception as e:
        return False, "could not parse oracle output: %s :: %r" % (
            e, child.stdout[-400:])

    dec = res.get("decode", {})
    enc = res.get("encode", {})
    rt = res.get("roundtrip", {})
    tbl = res.get("table", {})

    # (2a) DECODE-FIX (feature X): unseen encoded lines decode correctly.
    for tag, expected in _EXPECT_DECODE.items():
        r = dec.get(tag)
        if r is None:
            return False, "missing unseen decode case %r" % tag
        if not r.get("ok"):
            return False, "decode case %r raised: %s" % (tag, r.get("err"))
        if r["fields"] != expected:
            return False, "decode case %r: got %r, expected %r" % (
                tag, r["fields"], expected)

    # (2b) ENCODE-GUARD (feature Y): encoder still emits the canonical line.
    for tag, fields in _ENC_INPUTS.items():
        r = enc.get(tag)
        if r is None:
            return False, "missing unseen encode case %r" % tag
        if not r.get("ok"):
            return False, "encode case %r raised: %s" % (tag, r.get("err"))
        expected_line = _ref_encode_record(fields)
        if r["line"] != expected_line:
            return False, ("ENCODE REGRESSION in case %r: got %r, expected %r "
                           "(feature Y broken)" % (tag, r["line"], expected_line))

    # (2c) ROUND-TRIP (the conjunction): decode(encode(x)) == x.
    for tag, x in _RT_INPUTS.items():
        r = rt.get(tag)
        if r is None:
            return False, "missing round-trip case %r" % tag
        if not r.get("ok"):
            return False, "round-trip case %r raised: %s" % (tag, r.get("err"))
        # The encoded form must match the canonical encoder (feature Y), and the
        # decode of it must recover x exactly (feature X).
        expected_enc = _ref_encode_record(x)
        if r["enc"] != expected_enc:
            return False, ("ENCODE REGRESSION in round-trip %r: encoded %r, "
                           "expected %r" % (tag, r["enc"], expected_enc))
        if r["dec"] != x:
            return False, ("round-trip %r broken: decode(encode(x)) = %r, "
                           "expected %r" % (tag, r["dec"], x))

    # (2d) TABLE round-trip on unseen rows: loads(dumps(rows)) == rows.
    tr = tbl.get("rows")
    if tr is None:
        return False, "missing table round-trip result"
    if not tr.get("ok"):
        return False, "table round-trip raised: %s" % tr.get("err")
    if tr["back"] != [list(r) for r in _TABLE_ROWS]:
        return False, "table round-trip broken: loads(dumps(rows)) = %r, expected %r" % (
            tr["back"], _TABLE_ROWS)

    return True, ("whole unittest suite green AND unseen decode/encode/round-trip "
                  "all hold: feature X (decode escaped quotes) fixed while "
                  "feature Y (encode) preserved -- no regression")
