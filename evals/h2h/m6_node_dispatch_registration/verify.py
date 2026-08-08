"""Independent oracle for the miniast TryStar node-registration task.

Derived from the astroid FAIL_TO_PASS contract (pylint-dev/astroid #2142):
adding a node type means it must be registered CONSISTENTLY across the three
parallel dispatch tables (the node-class registry, the rebuilder, and the
AsStringVisitor). This oracle:

  (A) BEHAVIOR: drives the real package in a subprocess with FRESH raw trees
      defined HERE (the agent never saw them) -- a try/except* must round-trip
      to source with ``except*`` headers, nested handlers must work, and the
      rebuilt node must be an instance of the registered TryStar class;
  (B) CONSISTENCY: TryStar must be present in ALL THREE tables -- a partial
      registration (e.g. only the rebuilder, or only as_string) must FAIL;
  (C) REGRESSION: plain Try/Pass/Assign still round-trip unchanged;
  (D) DISTRACTORS: raw.py and version.py are byte-identical to the seed.

verify.py is NOT a file the agent is asked to touch.
"""
import importlib.util
import os
import re
import subprocess
import sys


def _pkg(workdir):
    return os.path.join(workdir, "miniast")


def _read(path):
    with open(path) as f:
        return f.read()


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# (A) Behavioral test: run against the real package with FRESH raw trees.
# ---------------------------------------------------------------------------
_BEHAVIOR = r'''
import sys, json
sys.path.insert(0, {workdir!r})

from miniast import roundtrip, Rebuilder, to_source
from miniast import nodes as N
from miniast.rebuilder import UnsupportedNodeError
from miniast.as_string import MissingVisitorError

out = {{}}

def _name(i): return {{"kind": "Name", "id": i}}
def _const(v): return {{"kind": "Const", "value": v}}

# A fresh try/except* tree the agent never saw: two except* handlers, one bare,
# plus an assignment in the body and a handler that binds a name.
raw_trystar = {{
    "kind": "TryStar",
    "body": [
        {{"kind": "Assign", "target": _name("x"), "value": _const(1)}},
        {{"kind": "Pass"}},
    ],
    "handlers": [
        {{"kind": "ExceptHandler", "type": _name("ValueError"), "name": "e",
          "body": [{{"kind": "Pass"}}]}},
        {{"kind": "ExceptHandler", "type": _name("TypeError"), "name": None,
          "body": [{{"kind": "Assign", "target": _name("y"),
                     "value": _const(2)}}]}},
        {{"kind": "ExceptHandler", "type": None, "name": None,
          "body": [{{"kind": "Pass"}}]}},
    ],
}}

# A plain Try with the SAME shape -- used to prove TryStar renders DIFFERENTLY
# (except* vs except) and that plain Try is untouched.
raw_try = dict(raw_trystar)
raw_try = {{
    "kind": "Try",
    "body": [{{"kind": "Pass"}}],
    "handlers": [
        {{"kind": "ExceptHandler", "type": _name("ValueError"), "name": "e",
          "body": [{{"kind": "Pass"}}]}},
    ],
}}

errors = []

# 1) rebuilder must not raise UnsupportedNodeError for TryStar
try:
    node = Rebuilder().visit(raw_trystar)
    out["rebuilt_class"] = type(node).__name__
    out["rebuilt_is_trystar"] = (type(node) is getattr(N, "TryStar", object))
    out["rebuilt_in_registry"] = (
        getattr(N, "TryStar", None) is not None
        and type(node) in N.ALL_NODE_CLASSES
    )
    # children must be rebuilt into real nodes, not left as raw dicts
    out["body_classes"] = [type(s).__name__ for s in node.body]
    out["handler_classes"] = [type(h).__name__ for h in node.handlers]
    out["handler_fields_ok"] = all(
        type(h).__name__ == "ExceptHandler"
        and all(type(s).__name__ != "dict" for s in h.body)
        for h in node.handlers
    )
except UnsupportedNodeError as ex:
    errors.append("rebuilder UnsupportedNodeError: %s" % ex)
except Exception as ex:
    errors.append("rebuilder raised %r" % ex)

# 2) full round-trip raw -> nodes -> source
try:
    out["trystar_source"] = roundtrip(raw_trystar)
except Exception as ex:
    errors.append("roundtrip(trystar) raised %r" % ex)

# 3) plain Try round-trip (regression)
try:
    out["try_source"] = roundtrip(raw_try)
except Exception as ex:
    errors.append("roundtrip(try) raised %r" % ex)

# 4) simple statements still work (regression)
try:
    out["pass_source"] = roundtrip({{"kind": "Module",
        "body": [{{"kind": "Pass"}}]}})
    out["assign_source"] = roundtrip({{"kind": "Module",
        "body": [{{"kind": "Assign", "target": _name("a"),
                   "value": _const(3)}}]}})
except Exception as ex:
    errors.append("simple roundtrip raised %r" % ex)

# 5) TryStar nested directly inside a Module (proves Module body dispatch works)
try:
    out["module_source"] = roundtrip({{"kind": "Module",
        "body": [raw_trystar]}})
except Exception as ex:
    errors.append("module-with-trystar roundtrip raised %r" % ex)

# 6) is_supported(TryStar) via the registry helper
try:
    out["is_supported_trystar"] = bool(
        getattr(N, "TryStar", None) is not None
        and N.is_supported(N.TryStar)
    )
except Exception as ex:
    errors.append("is_supported(TryStar) raised %r" % ex)

out["errors"] = errors
print("JSON_START")
print(json.dumps(out, default=str))
'''


def _run_behavior(workdir):
    script = _BEHAVIOR.format(workdir=workdir)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return None, "behavioral test crashed:\n" + (proc.stderr or proc.stdout)
    out = proc.stdout
    if "JSON_START" not in out:
        return None, "behavioral test produced no JSON:\n" + out
    import json
    blob = out.split("JSON_START", 1)[1].strip()
    try:
        return json.loads(blob), ""
    except Exception as e:  # noqa: BLE001
        return None, "could not parse behavioral JSON (%r):\n%s" % (e, blob)


def _check_behavior(workdir):
    data, err = _run_behavior(workdir)
    if data is None:
        return False, err
    if data.get("errors"):
        return False, "behavioral probe collected errors: %r" % (data.get("errors"),)

    # --- rebuilder produced a real, registered TryStar node ---
    if data.get("rebuilt_class") != "TryStar":
        return False, ("rebuilder must build a TryStar node (got class %r); "
                       "Rebuilder.visit_TryStar missing or wrong" % (data.get("rebuilt_class"),))
    if not data.get("rebuilt_is_trystar"):
        return False, "rebuilt node is not an instance of nodes.TryStar"
    if not data.get("rebuilt_in_registry"):
        return False, ("nodes.TryStar is not in ALL_NODE_CLASSES; the node-class "
                       "registry table was not updated")
    if not data.get("is_supported_trystar"):
        return False, "is_supported(TryStar) is False; TryStar not registered in ALL_NODE_CLASSES"

    # children must be rebuilt into typed nodes, not left as raw dicts
    if data.get("body_classes") != ["Assign", "Pass"]:
        return False, ("TryStar body was not rebuilt into typed nodes "
                       "(got %r); reuse the same body rebuilding Try uses" % (data.get("body_classes"),))
    if data.get("handler_classes") != ["ExceptHandler", "ExceptHandler", "ExceptHandler"]:
        return False, ("TryStar handlers were not rebuilt into ExceptHandler nodes "
                       "(got %r)" % (data.get("handler_classes"),))
    if not data.get("handler_fields_ok"):
        return False, "TryStar handler bodies were left as raw dicts (not recursively rebuilt)"

    # --- round-trip source for the try/except* tree ---
    src = data.get("trystar_source")
    if not isinstance(src, str) or not src:
        return False, "roundtrip(trystar) produced no source"
    lines = src.split("\n")

    if lines[0] != "try:":
        return False, "TryStar must render its header as 'try:' (got %r)" % (lines[0],)

    # The body must be indented two spaces and contain the assignment + pass.
    if "  x = 1" not in lines:
        return False, "TryStar body not rendered/indented correctly; expected '  x = 1' in:\n%s" % (src,)
    if "  pass" not in lines:
        return False, "TryStar body missing indented 'pass':\n%s" % (src,)

    # Each handler must use 'except*' (star), NOT plain 'except'.
    handler_heads = [ln for ln in lines if ln.lstrip().startswith("except")]
    if not handler_heads:
        return False, "TryStar rendered no except handlers:\n%s" % (src,)
    for h in handler_heads:
        if not re.match(r"^except\*", h):
            return False, ("TryStar handlers must render with 'except*' (star), "
                           "got header %r in:\n%s" % (h, src))
    # Specifically the bound/typed/bare forms.
    if "except* ValueError as e:" not in src:
        return False, "expected 'except* ValueError as e:' in TryStar source:\n%s" % (src,)
    if "except* TypeError:" not in src:
        return False, "expected 'except* TypeError:' (no 'as') in TryStar source:\n%s" % (src,)
    if "except*:" not in src:
        return False, "expected bare 'except*:' in TryStar source:\n%s" % (src,)
    # There must be NO plain (non-star) except under a TryStar.
    if re.search(r"(?m)^except[^*]", src) or re.search(r"(?m)^except:$", src):
        return False, "TryStar rendered a plain 'except' (no star) somewhere:\n%s" % (src,)

    # --- regression: plain Try must STILL render plain 'except' (no star) ---
    tsrc = data.get("try_source")
    if not isinstance(tsrc, str) or not tsrc:
        return False, "roundtrip(try) produced no source"
    if tsrc.split("\n")[0] != "try:":
        return False, "plain Try header changed: %r" % (tsrc.split("\n")[0],)
    if "except ValueError as e:" not in tsrc:
        return False, ("plain Try must still render plain 'except ValueError as e:' "
                       "(got):\n%s" % (tsrc,))
    if "except*" in tsrc:
        return False, ("plain Try wrongly rendered 'except*' -- TryStar's star "
                       "leaked into the plain Try/ExceptHandler path:\n%s" % (tsrc,))

    # --- regression: simple statements (a Module renders its top-level body
    # without indentation; suites inside try/except are indented two spaces) ---
    if data.get("pass_source") != "pass":
        return False, "Pass regression: expected 'pass', got %r" % (data.get("pass_source"),)
    if data.get("assign_source") != "a = 3":
        return False, "Assign regression: expected 'a = 3', got %r" % (data.get("assign_source"),)

    # --- TryStar nested inside a Module renders (header at module column 0,
    # its own body indented one level, handlers using except*) ---
    msrc = data.get("module_source")
    if not isinstance(msrc, str) or not msrc.startswith("try:"):
        return False, "TryStar nested in a Module did not render at module top level:\n%s" % (msrc,)
    if "  x = 1" not in msrc:
        return False, "TryStar-in-Module body not indented one level:\n%s" % (msrc,)
    if "except* ValueError as e:" not in msrc:
        return False, "TryStar handlers under a Module not rendered with except*:\n%s" % (msrc,)

    return True, "behavior OK"


# ---------------------------------------------------------------------------
# (B) Consistency: TryStar registered in ALL THREE tables (static check so a
#     partial fix is caught even before behavior).
# ---------------------------------------------------------------------------
def _check_consistency(workdir):
    pkg = _pkg(workdir)
    nodes_src = _read(os.path.join(pkg, "nodes.py"))
    reb_src = _read(os.path.join(pkg, "rebuilder.py"))
    asstr_src = _read(os.path.join(pkg, "as_string.py"))

    missing = []
    # Table #1: a TryStar class AND its presence in ALL_NODE_CLASSES.
    if not re.search(r"\bclass\s+TryStar\b", nodes_src):
        missing.append("nodes.py defines no `class TryStar`")
    else:
        m = re.search(r"ALL_NODE_CLASSES\s*=\s*\(([^)]*)\)", nodes_src, re.S)
        if not m or not re.search(r"\bTryStar\b", m.group(1)):
            missing.append("nodes.py: TryStar not added to ALL_NODE_CLASSES")

    # Table #2: rebuilder visitor.
    if not re.search(r"\bdef\s+visit_TryStar\s*\(\s*self\s*,", reb_src):
        missing.append("rebuilder.py defines no `visit_TryStar`")

    # Table #3: as_string visitor.
    if not re.search(r"\bdef\s+visit_TryStar\s*\(\s*self\s*,", asstr_src):
        missing.append("as_string.py defines no `visit_TryStar`")

    if missing:
        return False, ("TryStar registration is INCONSISTENT across the three "
                       "dispatch tables:\n  - " + "\n  - ".join(missing))
    return True, "TryStar registered in all three tables"


# ---------------------------------------------------------------------------
# (C) Distractors byte-identical to the freshly-seeded version.
# ---------------------------------------------------------------------------
def _seed_distractor_bytes():
    """Re-run THIS scenario's setup.py into a throwaway temp dir and return the
    seed bytes of the distractor files. Guarantees true byte-identity rather
    than relying on content needles."""
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "m6_seed_setup", os.path.join(here, "setup.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tmp = tempfile.mkdtemp(prefix="m6_seed_")
    mod.setup(tmp)
    return {
        "miniast/raw.py": _read_bytes(os.path.join(tmp, "miniast", "raw.py")),
        "miniast/version.py": _read_bytes(os.path.join(tmp, "miniast", "version.py")),
    }


def _check_distractors_unchanged(workdir):
    try:
        seed = _seed_distractor_bytes()
    except Exception as e:  # noqa: BLE001
        return False, "could not reseed distractor baseline: %r" % (e,)
    for rel, want in seed.items():
        path = os.path.join(workdir, rel)
        if not os.path.exists(path):
            return False, "distractor file removed: %s" % (rel,)
        if _read_bytes(path) != want:
            return False, ("distractor %s was modified; it must stay "
                           "byte-identical to the seed" % (rel,))
    return True, "distractors byte-identical"


# ---------------------------------------------------------------------------
# Syntax sanity across the package.
# ---------------------------------------------------------------------------
def _check_syntax(workdir):
    import ast
    pkg = _pkg(workdir)
    for root, _dirs, files in os.walk(pkg):
        for fn in files:
            if fn.endswith(".py"):
                p = os.path.join(root, fn)
                try:
                    ast.parse(_read(p))
                except SyntaxError as e:
                    return False, "syntax error in %s: %s" % (os.path.relpath(p, workdir), e)
    return True, "syntax ok"


def verify(workdir):
    checks = [
        ("syntax", _check_syntax),
        ("consistency", _check_consistency),
        ("distractors_unchanged", _check_distractors_unchanged),
        ("behavior", _check_behavior),
    ]
    for name, fn in checks:
        ok, detail = fn(workdir)
        if not ok:
            return False, "[%s] %s" % (name, detail)
    return True, ("all checks passed: TryStar registered consistently across "
                  "nodes.ALL_NODE_CLASSES + Rebuilder + AsStringVisitor and "
                  "round-trips to source with except*")
