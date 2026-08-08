import os


# The planted bug and its correct form (see setup.py). The reference fix reverts
# exactly the escaped-quote block in dsv.decode_record: restore the peek-ahead
# that treats a doubled quote ("") inside a quoted field as one literal quote,
# instead of letting the first quote close the field. This is a TARGETED change
# to the decoder ONLY -- the encoder is left untouched, so encoding does not
# regress.
_BUG = '''\
            if c == quote:
                # A lone quote closes the quoted run.
                in_quotes = False
                i += 1
                continue
'''

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


def apply(workdir):
    """Apply the CORRECT escaped-quote handling to dsv.decode_record.

    Reverts the planted decoder bug so a doubled quote inside a quoted field is
    decoded as one literal quote, restoring round-trip with the (already
    correct) encoder. Touches the decoder only; encoding is unchanged.
    """
    path = os.path.join(workdir, "dsv.py")
    with open(path, "r") as f:
        text = f.read()

    if _GOOD in text and _BUG not in text:
        return  # already correct

    if _BUG not in text:
        raise RuntimeError(
            "expected planted bug fragment not found in dsv.py; "
            "cannot apply reference fix"
        )

    text = text.replace(_BUG, _GOOD, 1)
    with open(path, "w") as f:
        f.write(text)
