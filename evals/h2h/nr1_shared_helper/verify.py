"""Independent oracle for the no-regression probe (nr1_shared_helper).

get_setting() must change from returning a value to returning a (value, source) tuple. Success is
SWE-bench-style: FAIL_TO_PASS (the requested change is made correctly) AND PASS_TO_PASS (every caller
still works — especially the DISTANT jobs/scheduler.py the task does not name). A partial fix that
updates the definition + the obvious caller but misses the distant one passes FAIL_TO_PASS and FAILS
PASS_TO_PASS (the 'fixed-but-broke-something' regression). The unrelated util.get_setting_label
distractor must be untouched.
"""
import subprocess
import sys


_BEHAVIOR = r'''
import sys, os, json
sys.path.insert(0, {workdir!r})
out = {{}}
try:
    from settingskit.core.config import get_setting
    # FAIL_TO_PASS: (value, source) tuple with the right source per layer, same values as before.
    rf = get_setting("workers")                 # file store
    out["is_tuple"] = isinstance(rf, tuple) and len(rf) == 2
    out["file"] = list(rf) if isinstance(rf, tuple) else rf
    os.environ["db_url"] = "postgres://x"
    re_ = get_setting("db_url")                  # env override
    out["env"] = list(re_) if isinstance(re_, tuple) else re_
    os.environ.pop("db_url", None)
    rd = get_setting("totally_unknown_key")      # default fallback
    out["default"] = list(rd) if isinstance(rd, tuple) else rd
except Exception as e:
    out["import_error"] = "%s: %s" % (type(e).__name__, e)

# PASS_TO_PASS: callers must still produce their original behavior (break if not updated to the tuple).
callers = {{}}
for name, mod, fn, expect in [
    ("max_workers", "settingskit.jobs.scheduler", "max_workers", 4),
    ("db_banner", "settingskit.app.api", "db_banner", "DB=sqlite:///app.db"),
    ("timeout_line", "settingskit.app.report", "timeout_line", "timeout=30s"),
]:
    try:
        m = __import__(mod, fromlist=[fn])
        got = getattr(m, fn)()
        callers[name] = [got == expect, repr(got)]
    except Exception as e:
        callers[name] = [False, "EXC:%s:%s" % (type(e).__name__, e)]
out["callers"] = callers

try:
    from settingskit.util.strings import get_setting_label
    out["distractor_ok"] = (get_setting_label({{"a": 1}}, "a") == "a=1")
except Exception as e:
    out["distractor_ok"] = False
    out["distractor_err"] = str(e)

print("JSON_START")
print(json.dumps(out, default=str))
'''


def verify(workdir):
    script = _BEHAVIOR.format(workdir=workdir)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    if "JSON_START" not in proc.stdout:
        return False, "behavioral test produced no JSON:\n" + (proc.stderr or proc.stdout)[-600:]
    import json
    data = json.loads(proc.stdout.split("JSON_START", 1)[1].strip())

    if data.get("import_error"):
        return False, "import/return error: " + data["import_error"]
    # FAIL_TO_PASS — the requested change
    if not data.get("is_tuple"):
        return False, "get_setting must return a (value, source) tuple; got %r" % (data.get("file"),)
    if data.get("file") != ["4", "file"]:
        return False, "file-store lookup must be ('4', 'file'); got %r" % (data.get("file"),)
    if data.get("env") != ["postgres://x", "env"]:
        return False, "env override must be (value, 'env'); got %r" % (data.get("env"),)
    if data.get("default") != ["", "default"]:
        return False, "default fallback must be ('', 'default'); got %r" % (data.get("default"),)
    # PASS_TO_PASS — no regression in any caller (the distant scheduler is the discriminator)
    broke = [n for n, (ok, _g) in data.get("callers", {}).items() if not ok]
    if broke:
        det = "; ".join("%s -> %s" % (n, data["callers"][n][1]) for n in broke)
        return False, "REGRESSION — caller(s) broke after the change (re-observation-reach miss): " + det
    # distractor must be intact
    if not data.get("distractor_ok"):
        return False, "util.get_setting_label distractor was broken/edited: " + str(data.get("distractor_err", ""))
    return True, "all checks passed: get_setting returns (value, source) AND every caller still works (no regression)"
