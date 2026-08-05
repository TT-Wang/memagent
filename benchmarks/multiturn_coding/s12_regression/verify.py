import subprocess
import sys

CHECKS = [("import mathlib.seq as q; assert q.__doc__ and 'normalize' in q.__doc__", 'docstring'), ('import mathlib.stats as st; assert st.median([3, 1, 2]) == 2 and st.median([4, 1, 3, 2]) == 2.5', 'median'), ('import mathlib.seq as q; assert q.normalize((3, 1, 2)) == [1, 2, 3] and q.normalize(x for x in [2, 1]) == [1, 2]', 'normalize inputs'), ('import mathlib.seq as q; assert q.clamp([0, 5, 10], 1, 8) == [1, 5, 8]', 'clamp'), ('import mathlib.seq as q; assert q.scale([1, 2], 0) == [0, 0]', 'scale k=0'), ("import mathlib.seq as q; r = q.normalize([2.0, float('nan'), 1.0]); assert r == [1.0, 2.0]", 'normalize NaN'), ('import mathlib; assert mathlib.VERSION', 'version'), ('import mathlib.stats as st; assert st.percentile([1, 2, 3, 4], 50) in (2, 2.5, 3)', 'percentile'), ('import mathlib.seq as q; assert q.normalize([2, 1, 2]) == [1, 2]', 'normalize perf'), ('import mathlib.seq as q; assert q.top_k([1, 3, 2], 2) == [3, 2]', 'top_k'), ("import subprocess, sys; out = subprocess.run([sys.executable, '-m', 'mathlib.cli', 'summary', '1', '2', '3'], capture_output=True, text=True).stdout; assert 'mean' in out", 'cli'), ("import os; assert os.path.isfile('CHANGELOG.md')", 'changelog')]
REGRESS = [('import mathlib.seq as q; a = [3, 1, 2]; r = q.normalize(a); assert r == [1, 2, 3] and a == [3, 1, 2] and r is not a', 'normalize returns a NEW list, input untouched (stats.py depends on it)'), ('import mathlib.seq as q; r = q.normalize([2, 1, 2, 1]); assert r == [1, 2]', 'normalize dedupes ascending (report.py label map depends on it)'), ("import mathlib.stats as st; s = st.summary([1, 2, 3]); assert set(s) >= {'mean', 'lo', 'hi'} and s['mean'] == 2 and s['lo'] == 1", 'stats.summary key names + values (report.py reads these keys)'), ('import mathlib.seq as q; assert len(q.scale([1, 2, 3], 2)) == 3 and q.scale([1, 2, 3], 2) == [2, 4, 6]', 'scale preserves length and order'), ("import mathlib.report as rp; text = rp.render([3, 1, 2]); assert 'mean=2' in text and 'range=1..3' in text", 'report.render end-to-end format (the distant caller chain)')]


def _run(root, probe):
    r = subprocess.run([sys.executable, "-c", probe], cwd=root,
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def verify(root):
    missing = [label for probe, label in CHECKS if not _run(root, probe)]
    broken = [label for probe, label in REGRESS if not _run(root, probe)]
    ok = not missing and not broken
    detail = ("all {} features + {} frozen behaviors hold".format(len(CHECKS), len(REGRESS))
              if ok else
              ("REGRESS: " + "; ".join(broken) + " | " if broken else "")
              + ("missing: " + "; ".join(missing) if missing else ""))
    return ok, detail
