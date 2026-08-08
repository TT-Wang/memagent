import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def setup(workdir):
    """Build a small, coherent sales-reporting library and plant ONE root-cause
    bug two call-frames below the symptom.

    Package layout (a real, non-toy mini library):

        salesreport/
            __init__.py        - public re-exports
            report.py          - public API: summarize(...) / leaderboard(...)
                                 (this is where the traceback SURFACES)
            aggregate.py       - group_sum(...) / distinct_groups(...)
                                 (the MIDDLE frame; structurally correct)
            fields.py          - normalize_group(...) / coerce_number(...)
                                 (the DEEPEST frame; the ROOT CAUSE lives here)

    Call chain for the failing operation:

        report.summarize -> aggregate.group_sum -> fields.normalize_group

    The ROOT CAUSE: ``fields.normalize_group`` canonicalizes a grouping key by
    calling ``value.strip().lower()`` unconditionally. That works when the
    grouping column holds strings, but a real dataset has numeric region codes
    (ints) and occasional missing values (None) in the grouping column, and on
    the first such row the deepest frame raises::

        AttributeError: 'int' object has no attribute 'strip'

    The SYMPTOM file (report.py) and the MIDDLE file (aggregate.py) look
    perfectly correct in isolation -- the defect is two frames down. A correct
    fix coerces the value to text (and treats None as the empty group) INSIDE
    ``normalize_group`` so every caller of that shared helper benefits; a
    band-aid placed only in ``aggregate.group_sum`` (e.g. wrapping the call in a
    try/except, or pre-stringifying just before that one call site) leaves the
    OTHER caller, ``aggregate.distinct_groups`` -> report.leaderboard, still
    crashing on the same data, which the oracle exercises.
    """

    _w(workdir, os.path.join("salesreport", "__init__.py"), '''\
"""salesreport: a tiny sales-rollup library.

Records are plain dicts (rows). The library groups rows by a chosen field and
rolls up a numeric field, and can also list the distinct group labels.

Public API:
    summarize(rows, group_field, value_field) -> {group_label: total}
    leaderboard(rows, group_field, value_field, top=3) -> [(label, total), ...]

The grouping label is CANONICALIZED (trimmed + lowercased) so that, e.g.,
'North', ' north ' and 'NORTH' all roll up into the same bucket. That
canonicalization lives in salesreport.fields.normalize_group and is shared by
every code path that needs a group label.
"""
from .report import summarize, leaderboard
from .fields import normalize_group, coerce_number

__all__ = ["summarize", "leaderboard", "normalize_group", "coerce_number"]
''')

    # ------------------------------------------------------------------ report.py
    # PUBLIC API. This is the file the traceback POINTS AT first (top frame),
    # but it is structurally correct: it merely delegates to aggregate.py.
    _w(workdir, os.path.join("salesreport", "report.py"), '''\
"""Public reporting API.

These functions are thin orchestration over :mod:`salesreport.aggregate`.
They do not themselves touch the grouping-key canonicalization; that happens
two layers down in :mod:`salesreport.fields`.
"""
from .aggregate import group_sum, distinct_groups


def summarize(rows, group_field, value_field):
    """Roll up ``value_field`` per distinct value of ``group_field``.

    Returns a dict ``{canonical_group_label: total}``. ``rows`` is an iterable
    of dict records; ``group_field`` / ``value_field`` are the dict keys to use.
    """
    return group_sum(rows, group_field, value_field)


def leaderboard(rows, group_field, value_field, top=3):
    """Return the ``top`` groups by rolled-up total, descending.

    Ties break by group label (ascending) for a stable, deterministic order.
    """
    totals = group_sum(rows, group_field, value_field)
    # distinct_groups is also exercised so the ordering is over the SAME
    # canonical labels the rollup used.
    labels = distinct_groups(rows, group_field)
    ranked = sorted(
        ((label, totals.get(label, 0.0)) for label in labels),
        key=lambda lt: (-lt[1], lt[0]),
    )
    return ranked[:top]
''')

    # --------------------------------------------------------------- aggregate.py
    # MIDDLE frame. Structurally correct: it calls the shared canonicalizer and
    # number coercion from fields.py. It does NOT special-case any value type.
    _w(workdir, os.path.join("salesreport", "aggregate.py"), '''\
"""Aggregation primitives.

Both functions canonicalize the grouping label via
:func:`salesreport.fields.normalize_group` so that values that differ only by
surrounding whitespace or letter case collapse into one bucket. The numeric
value is parsed via :func:`salesreport.fields.coerce_number`.
"""
from .fields import normalize_group, coerce_number


def group_sum(rows, group_field, value_field):
    """Sum ``value_field`` grouped by the canonical ``group_field`` label."""
    totals = {}
    for row in rows:
        label = normalize_group(row.get(group_field))
        amount = coerce_number(row.get(value_field))
        totals[label] = totals.get(label, 0.0) + amount
    return totals


def distinct_groups(rows, group_field):
    """Return the sorted set of canonical group labels present in ``rows``."""
    seen = set()
    for row in rows:
        seen.add(normalize_group(row.get(group_field)))
    return sorted(seen)
''')

    # ------------------------------------------------------------------ fields.py
    # DEEPEST frame -- the ROOT CAUSE. normalize_group assumes the value is
    # always a str and calls .strip().lower() directly. A numeric group code or
    # a missing (None) group value raises AttributeError two frames below the
    # public call. coerce_number is fine and is here only as a realistic
    # sibling helper.
    _w(workdir, os.path.join("salesreport", "fields.py"), '''\
"""Field-level helpers: canonicalizing a group label and parsing a number.

This is the lowest layer of the library. Everything that needs a *canonical*
group label goes through :func:`normalize_group`, and everything that needs a
numeric value goes through :func:`coerce_number`.
"""


def normalize_group(value):
    """Canonicalize a grouping key.

    The canonical form is the value with surrounding whitespace stripped and
    letters lower-cased, so 'North', ' north ' and 'NORTH' all map to 'north'.
    """
    # NOTE: assumes ``value`` is a string. Real datasets, however, carry numeric
    # region codes and the occasional missing cell in the grouping column.
    return value.strip().lower()


def coerce_number(value):
    """Parse ``value`` into a float, treating None/'' as 0.0.

    Accepts ints/floats as-is and numeric strings (with optional surrounding
    whitespace). Anything unparseable counts as 0.0 so one bad cell does not
    abort a whole report.
    """
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0
''')

    # ------------------------------------------------------------------ repro.py
    # A runnable repro the user can point at. It feeds a dataset whose grouping
    # column mixes strings AND integer region codes, so the deepest frame blows
    # up. (The oracle uses DIFFERENT data the agent never sees.)
    _w(workdir, "repro.py", '''\
"""Minimal reproduction of the crash.

    python repro.py

The dataset's "region" column mixes textual names with numeric region codes
(an int) -- exactly what comes out of the upstream export. summarize() raises.
"""
from salesreport import summarize

ROWS = [
    {"region": "North", "amount": "100"},
    {"region": " north ", "amount": "50"},
    {"region": 7, "amount": "20"},          # numeric region code from the export
    {"region": "South", "amount": "30"},
]

if __name__ == "__main__":
    print(summarize(ROWS, "region", "amount"))
''')

    # ------------------------------------------------------------------ README
    _w(workdir, "README.md", '''\
# salesreport

A tiny library that rolls up record dicts by a grouping field.

```python
from salesreport import summarize, leaderboard

rows = [
    {"region": "North", "amount": "100"},
    {"region": " north ", "amount": "50"},
    {"region": "South", "amount": "30"},
]
summarize(rows, "region", "amount")     # {'north': 150.0, 'south': 30.0}
leaderboard(rows, "region", "amount")   # [('north', 150.0), ('south', 30.0)]
```

Group labels are canonicalized (trimmed + lowercased) so that values differing
only by whitespace or case collapse into one bucket.

## Observed problem

Our upstream export sometimes emits **numeric region codes** (an int) and the
occasional **missing region** (a blank cell) in the grouping column. On such
data, `summarize()` and `leaderboard()` crash instead of bucketing the row.
See `repro.py` for a runnable example; running it produces the traceback below.
''')

    # ---------------------------------------------------------------- TRACEBACK
    # Captured separately so the prompt can paste the REAL traceback text.
    _w(workdir, "TRACEBACK.txt", '''\
Traceback (most recent call last):
  File "repro.py", line 18, in <module>
    print(summarize(ROWS, "region", "amount"))
          ~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "salesreport/report.py", line 16, in summarize
    return group_sum(rows, group_field, value_field)
  File "salesreport/aggregate.py", line 15, in group_sum
    label = normalize_group(row.get(group_field))
  File "salesreport/fields.py", line 17, in normalize_group
    return value.strip().lower()
           ^^^^^^^^^^^
AttributeError: 'int' object has no attribute 'strip'
''')
