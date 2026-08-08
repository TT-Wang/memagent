import os


# The planted root cause sits in salesreport/fields.py: normalize_group calls
# ``value.strip().lower()`` directly, which raises on a non-string (numeric code)
# or None grouping value. The correct fix coerces to text INSIDE the shared
# helper -- None becomes the empty-string group, everything else is stringified
# first -- so every caller (group_sum AND distinct_groups) is fixed at once.
_BUG = '''\
    # NOTE: assumes ``value`` is a string. Real datasets, however, carry numeric
    # region codes and the occasional missing cell in the grouping column.
    return value.strip().lower()'''

_GOOD = '''\
    # Coerce to text before canonicalizing: a missing value collapses into the
    # empty-string group, and numeric region codes are normalized by their text
    # form (e.g. 7 -> "7"). This is the single shared canonicalizer, so fixing
    # it here repairs every caller at once.
    if value is None:
        value = ""
    return str(value).strip().lower()'''


def apply(workdir):
    """Apply the correct root-cause fix to a fresh setup workdir.

    Rewrites ``salesreport/fields.py::normalize_group`` so the shared
    canonicalizer coerces its argument to text before trimming/lowercasing.
    """
    path = os.path.join(workdir, "salesreport", "fields.py")
    with open(path, "r") as f:
        text = f.read()

    if _GOOD in text and _BUG not in text:
        return  # already correct

    if _BUG not in text:
        raise RuntimeError(
            "expected planted normalize_group body not found in "
            "salesreport/fields.py; cannot apply reference fix"
        )

    text = text.replace(_BUG, _GOOD, 1)
    with open(path, "w") as f:
        f.write(text)
