import os
import re
import sys
import json
import subprocess


# Independent, outcome-based oracle. We do NOT import setup.py and we do NOT
# trust any value the agent typed from memory: the ground truth is whatever the
# `plugins/*.py` modules ACTUALLY declare on disk right now, with the naming rule
# applied. We then import the agent's registry.py in a FRESH subprocess and check
# it agrees with that ground truth exactly, and that the one violator was fixed.

PKG = "plugins"

# A valid capability token: lowercase snake_case beginning with "cap_".
_VALID = re.compile(r"^cap_[a-z0-9_]+$")

# The CAPABILITY assignment we read from each module's source.
_CAP_RE = re.compile(r'''^\s*CAPABILITY\s*=\s*["']([^"']*)["']''', re.M)


def _canonicalize(tok):
    """The naming-rule fix: lowercase every letter and turn each '-' into '_'.
    Deterministic and matches NAMING_RULE.md."""
    return tok.lower().replace("-", "_")


def _read_disk_caps(workdir):
    """{stem: CAPABILITY-as-declared-on-disk} for every plugin module."""
    pkg_dir = os.path.join(workdir, PKG)
    caps = {}
    for fn in sorted(os.listdir(pkg_dir)):
        if not fn.endswith(".py") or fn == "__init__.py":
            continue
        stem = fn[:-3]
        with open(os.path.join(pkg_dir, fn)) as f:
            src = f.read()
        m = _CAP_RE.search(src)
        if not m:
            caps[stem] = None  # module without a CAPABILITY assignment -> oracle will flag
        else:
            caps[stem] = m.group(1)
    return caps


# Child that imports the agent's registry.py in a clean process and emits REGISTRY
# as JSON. Run with cwd=workdir so `import registry` resolves the agent's file.
_CHILD = r'''
import json, sys
try:
    import registry
except Exception as e:
    sys.stdout.write(json.dumps({"ok": False, "err": "%s: %s" % (type(e).__name__, e)}))
    sys.exit(0)
reg = getattr(registry, "REGISTRY", None)
if not isinstance(reg, dict):
    sys.stdout.write(json.dumps({"ok": False, "err": "registry.REGISTRY is not a dict (got %r)" % type(reg)}))
    sys.exit(0)
# Coerce keys/values to str so a stray non-str key doesn't crash JSON.
out = {}
for k, v in reg.items():
    out[str(k)] = v
sys.stdout.write(json.dumps({"ok": True, "registry": out}))
'''


def verify(workdir):
    pkg_dir = os.path.join(workdir, PKG)
    if not os.path.isdir(pkg_dir):
        return False, "plugins/ package not found in workdir"

    reg_path = os.path.join(workdir, "registry.py")
    if not os.path.isfile(reg_path):
        return False, "registry.py not found at repo root (the registry was not built)"

    # 1) Ground truth from the CURRENT on-disk module sources.
    disk = _read_disk_caps(workdir)
    if not disk:
        return False, "no plugin modules found under plugins/"

    missing_cap = [s for s, c in disk.items() if c is None]
    if missing_cap:
        return False, "module(s) missing a CAPABILITY assignment: %s" % ", ".join(sorted(missing_cap))

    # 2) Every on-disk value must NOW satisfy the naming rule. If any still does
    #    not, the violator was not fixed (or a new violation was introduced).
    bad_disk = sorted(s for s, c in disk.items() if not _VALID.match(c))
    if bad_disk:
        return False, ("module(s) still violate the naming rule on disk (not fixed): "
                       + ", ".join("%s=%r" % (s, disk[s]) for s in bad_disk))

    # 3) The expected registry = each stem -> its (now rule-compliant) on-disk value.
    expected = dict(disk)

    # 4) Import the agent's registry.py in a fresh subprocess and compare.
    pycache = os.path.join(workdir, "__pycache__")
    if os.path.isdir(pycache):
        for fn in os.listdir(pycache):
            if fn.startswith("registry.") and fn.endswith(".pyc"):
                try:
                    os.remove(os.path.join(pycache, fn))
                except OSError:
                    pass

    proc = subprocess.run(
        [sys.executable, "-B", "-c", _CHILD],
        cwd=workdir, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return False, "child crashed (rc=%d): %s" % (proc.returncode, (proc.stderr or proc.stdout)[-300:])
    try:
        res = json.loads(proc.stdout)
    except Exception as e:
        return False, "could not parse child output: %s :: %r" % (e, proc.stdout[-300:])
    if not res.get("ok"):
        return False, "registry.py not importable / malformed: %s" % res.get("err", "?")

    got = res["registry"]

    # 5a) No missing modules.
    miss = sorted(set(expected) - set(got))
    if miss:
        return False, "registry is INCOMPLETE — missing module(s): %s" % ", ".join(miss)
    # 5b) No extra/invented keys.
    extra = sorted(set(got) - set(expected))
    if extra:
        return False, "registry has extra/unknown key(s): %s" % ", ".join(extra)
    # 5c) Every value exact (catches a guessed/mis-transcribed token AND a registry
    #     that recorded the violator's OLD value instead of the fixed one).
    wrong = []
    for stem in sorted(expected):
        if got.get(stem) != expected[stem]:
            wrong.append("%s: registry=%r disk=%r" % (stem, got.get(stem), expected[stem]))
    if wrong:
        return False, "registry value(s) disagree with on-disk source: " + "; ".join(wrong)

    # 6) Every registry value also independently satisfies the naming rule
    #    (belt-and-suspenders; the disk check above already guarantees it).
    bad_vals = sorted(s for s in got if not _VALID.match(str(got[s])))
    if bad_vals:
        return False, "registry value(s) violate the naming rule: " + ", ".join(bad_vals)

    return True, ("registry complete & exact for all %d modules; violator fixed and "
                  "every CAPABILITY satisfies cap_ snake_case rule" % len(expected))
