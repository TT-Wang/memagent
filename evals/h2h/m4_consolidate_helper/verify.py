"""Independent oracle for the mailkit punycode-consolidation refactor.

Ported from SWE-bench django__django-11532 (Django ticket #30608). Checks:
  (A) BEHAVIOR (the real FAIL_TO_PASS): with a mocked non-ASCII local hostname
      and a non-unicode email encoding, EmailMessage.message() must NOT crash and
      the Message-ID must contain the host's Punycode (xn--...). Plus PASS_TO_PASS:
      ASCII hostnames still work and the validators / urlize still normalize IDNs.
  (B) CONSOLIDATION: a single punycode() helper exists in encoding.py and the raw
      idiom `X.encode('idna').decode('ascii')` survives in EXACTLY ONE place in the
      whole package (the helper) -- i.e. duplication is genuinely removed and no
      caller keeps its own inline copy.
  (C) BUG SITE: utils.CachedDnsName.get_fqdn now routes through punycode().
  (D) DISTRACTORS: http.py and version.py are byte-identical to the seed.

The behavioral portion runs in a subprocess against the real package and drives
it with inputs DEFINED HERE (handlers/hosts the agent never saw). This file is
not one the agent is asked to touch.
"""
import ast
import os
import re
import subprocess
import sys


def _pkg(workdir):
    return os.path.join(workdir, "mailkit")


def _read(path):
    with open(path) as f:
        return f.read()


# ---------------------------------------------------------------------------
# (A) Behavioral test: drive the real package in a subprocess with FRESH inputs.
# The non-ASCII host and the punycode targets are defined HERE, not in any seed
# file the agent edits.
# ---------------------------------------------------------------------------
_BEHAVIOR = r'''
import sys, json
from unittest import mock
sys.path.insert(0, {workdir!r})

import mailkit.utils as utils
from mailkit import EmailMessage, URLValidator, EmailValidator, urlize, ValidationError

out = {{}}

# --- The real FAIL_TO_PASS: non-ASCII hostname + non-unicode email encoding ---
# Mock the OS hostname lookup to a non-ASCII value and clear any cache so the
# package must (re)compute the fqdn through its own code path.
HOST = "漢字"          # the Han characters used in the real ticket
HOST_PUNY = "xn--p8s937b"      # its Punycode (IDN -> ACE)
try:
    if hasattr(utils.DNS_NAME, "_fqdn"):
        del utils.DNS_NAME._fqdn
except Exception:
    pass

with mock.patch("socket.getfqdn", return_value=HOST):
    try:
        if hasattr(utils.DNS_NAME, "_fqdn"):
            del utils.DNS_NAME._fqdn
    except Exception:
        pass
    email = EmailMessage("subject", "content", "from@example.com", ["to@example.com"])
    email.encoding = "iso-8859-1"
    try:
        msg = email.message()
        out["nonascii_crash"] = None
        out["message_id"] = msg.get("Message-ID")
    except Exception as e:  # noqa: BLE001
        out["nonascii_crash"] = "%s: %s" % (type(e).__name__, e)
        out["message_id"] = None

# --- PASS_TO_PASS: an ASCII hostname still produces a usable Message-ID ---
try:
    if hasattr(utils.DNS_NAME, "_fqdn"):
        del utils.DNS_NAME._fqdn
except Exception:
    pass
with mock.patch("socket.getfqdn", return_value="ascii.example.com"):
    try:
        if hasattr(utils.DNS_NAME, "_fqdn"):
            del utils.DNS_NAME._fqdn
    except Exception:
        pass
    e2 = EmailMessage("s", "b", "f@example.com", ["t@example.com"])
    e2.encoding = "iso-8859-1"
    try:
        m2 = e2.message()
        out["ascii_crash"] = None
        out["ascii_message_id"] = m2.get("Message-ID")
    except Exception as e:  # noqa: BLE001
        out["ascii_crash"] = "%s: %s" % (type(e).__name__, e)
        out["ascii_message_id"] = None

# --- PASS_TO_PASS: validators + urlize still normalize IDNs to ACE ---
uv = URLValidator()
ev = EmailValidator()
try:
    out["url_idn"] = uv("http://exämple.com/path")     # exae.. -> xn--exmple-cua.com
except Exception as e:  # noqa: BLE001
    out["url_idn"] = "ERR:%s" % (e,)
try:
    out["email_idn"] = ev("user@exämple.com")
except Exception as e:  # noqa: BLE001
    out["email_idn"] = "ERR:%s" % (e,)
out["urlize_idn"] = urlize("visit exämple.com today")

# URLValidator still rejects a bad scheme (existing behavior preserved).
try:
    uv("ftp://example.com")
    out["bad_scheme_rejected"] = False
except ValidationError:
    out["bad_scheme_rejected"] = True
except Exception:  # noqa: BLE001
    out["bad_scheme_rejected"] = False

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
        return None, "behavioral test raised:\n" + (proc.stderr or proc.stdout)
    out = proc.stdout
    if "JSON_START" not in out:
        return None, "behavioral test produced no JSON:\n" + out
    import json
    blob = out.split("JSON_START", 1)[1].strip()
    try:
        return json.loads(blob), ""
    except Exception as e:  # noqa: BLE001
        return None, "could not parse behavioral JSON (%r):\n%s" % (e, blob)


_PUNY_HAN = "xn--p8s937b"        # punycode of the Han host
_PUNY_EXAMPLE = "xn--exmple-cua.com"  # punycode of exämple.com


def _check_behavior(workdir):
    data, err = _run_behavior(workdir)
    if data is None:
        return False, err

    # The real FAIL_TO_PASS: non-ASCII host must not crash, and the Message-ID
    # must carry the host's Punycode label.
    if data.get("nonascii_crash"):
        return False, ("non-ASCII hostname under a non-unicode encoding still "
                       "crashes: %s. (utils.get_fqdn must punycode the hostname)"
                       % (data["nonascii_crash"],))
    mid = data.get("message_id") or ""
    if _PUNY_HAN not in mid:
        return False, ("Message-ID must contain the host's Punycode %r; got %r. "
                       "(the hostname was not IDN->ACE converted)"
                       % (_PUNY_HAN, mid))

    # PASS_TO_PASS: ASCII hostname still works.
    if data.get("ascii_crash"):
        return False, "ASCII hostname regressed (now crashes): %s" % (data["ascii_crash"],)
    a_mid = data.get("ascii_message_id") or ""
    if "ascii.example.com" not in a_mid:
        return False, "ASCII hostname Message-ID lost its domain: %r" % (a_mid,)

    # PASS_TO_PASS: validators + urlize still normalize IDNs.
    url_idn = data.get("url_idn") or ""
    if not isinstance(url_idn, str) or _PUNY_EXAMPLE not in url_idn:
        return False, "URLValidator no longer normalizes IDN host to ACE: %r" % (url_idn,)
    email_idn = data.get("email_idn") or ""
    if not isinstance(email_idn, str) or email_idn != "user@" + _PUNY_EXAMPLE:
        return False, "EmailValidator no longer normalizes IDN domain to ACE: %r" % (email_idn,)
    urlize_idn = data.get("urlize_idn") or ""
    if _PUNY_EXAMPLE not in urlize_idn:
        return False, "urlize no longer normalizes IDN host to ACE: %r" % (urlize_idn,)

    if not data.get("bad_scheme_rejected"):
        return False, "URLValidator stopped rejecting an unsupported scheme (behavior regressed)"

    return True, "behavior OK"


# ---------------------------------------------------------------------------
# (B) Consolidation: helper exists; the raw idiom survives in EXACTLY ONE place.
# ---------------------------------------------------------------------------
def _count_idna_idiom(src):
    """Count executable `X.encode('idna').decode('ascii')` calls (AST-based, so
    comments and string literals do NOT count)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    n = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "decode"):
            continue
        if not (node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "ascii"):
            continue
        inner = f.value
        if not isinstance(inner, ast.Call):
            continue
        g = inner.func
        if not (isinstance(g, ast.Attribute) and g.attr == "encode"):
            continue
        if (inner.args and isinstance(inner.args[0], ast.Constant)
                and inner.args[0].value == "idna"):
            n += 1
    return n


def _py_files(pkg):
    files = []
    for root, _dirs, names in os.walk(pkg):
        for fn in names:
            if fn.endswith(".py"):
                files.append(os.path.join(root, fn))
    return sorted(files)


def _has_punycode_helper(src):
    """True if src defines a top-level def punycode(domain) that returns the
    idna idiom (so the helper is the real one, not a stub)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "punycode":
            return _count_idna_idiom(ast.unparse(node)) == 1
    return False


def _check_consolidation(workdir):
    pkg = _pkg(workdir)
    enc = os.path.join(pkg, "encoding.py")
    if not os.path.exists(enc):
        return False, "encoding.py is missing"
    if not _has_punycode_helper(_read(enc)):
        return False, ("encoding.py must define a real helper "
                       "`def punycode(domain): return domain.encode('idna').decode('ascii')`")

    # The raw idiom must appear in EXACTLY ONE place across the whole package.
    total = 0
    per_file = {}
    for p in _py_files(pkg):
        c = _count_idna_idiom(_read(p))
        if c is None:
            return False, "syntax error while scanning %s" % (os.path.relpath(p, workdir),)
        per_file[os.path.relpath(p, workdir)] = c
        total += c
    if total != 1:
        offenders = {k: v for k, v in per_file.items() if v}
        return False, ("the raw idiom `X.encode('idna').decode('ascii')` must live in "
                       "EXACTLY ONE place (the punycode helper); found %d occurrence(s): %r. "
                       "(every caller must be rewired to punycode(); duplication must be removed)"
                       % (total, offenders))
    # ...and that one place must be encoding.py.
    if per_file.get(os.path.join("mailkit", "encoding.py")) != 1:
        return False, "the single surviving idiom must be inside encoding.py's punycode helper"

    return True, "consolidation OK"


def _imports_or_uses_punycode(src):
    """True if the module imports punycode and calls it."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    imported = False
    called = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "punycode":
                    imported = True
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "punycode":
                called = True
            if isinstance(f, ast.Attribute) and f.attr == "punycode":
                called = True
    return imported and called


def _check_callers_use_helper(workdir):
    pkg = _pkg(workdir)
    required = {
        "validators.py": os.path.join(pkg, "validators.py"),
        "message.py": os.path.join(pkg, "message.py"),
        "html.py": os.path.join(pkg, "html.py"),
        "utils.py": os.path.join(pkg, "utils.py"),
    }
    missing = []
    for label, path in required.items():
        if not os.path.exists(path) or not _imports_or_uses_punycode(_read(path)):
            missing.append(label)
    if missing:
        return False, ("these caller modules must import and call the shared punycode() "
                       "helper: %s" % (", ".join(missing),))
    return True, "callers use helper"


# ---------------------------------------------------------------------------
# (C) Bug site: utils.get_fqdn must route the hostname through punycode().
# ---------------------------------------------------------------------------
def _check_bug_site(workdir):
    pkg = _pkg(workdir)
    src = _read(os.path.join(pkg, "utils.py"))
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return False, "utils.py syntax error: %s" % (e,)
    # Find get_fqdn and assert it contains a call punycode(...) wrapping the
    # socket.getfqdn() lookup (not just a bare punycode anywhere).
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_fqdn":
            body_src = ast.unparse(node)
            if "punycode(" in body_src and "getfqdn" in body_src:
                # Ensure punycode wraps the getfqdn result, not a no-op constant.
                if re.search(r"punycode\s*\(\s*socket\.getfqdn\s*\(\s*\)\s*\)", body_src):
                    return True, "bug site fixed"
                # Allow an intermediate variable, but still require both present.
                if "punycode(" in body_src and "getfqdn" in body_src:
                    return True, "bug site fixed (via intermediate)"
            return False, ("utils.CachedDnsName.get_fqdn must convert the looked-up "
                           "hostname with punycode(socket.getfqdn()) -- this is the actual bug")
    return False, "utils.py no longer defines get_fqdn"


# ---------------------------------------------------------------------------
# (D) Distractors must be byte-identical to the seed.
# ---------------------------------------------------------------------------
def _seed_distractors():
    """Re-derive the exact seed bytes of the distractor files from setup.py so
    the check is a true byte-for-byte comparison, independent of the workdir."""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "m4_setup_for_verify", os.path.join(here, "setup.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import tempfile
    tmp = tempfile.mkdtemp(prefix="m4seed_")
    mod.setup(tmp)
    return {
        os.path.join("mailkit", "http.py"): _read(os.path.join(tmp, "mailkit", "http.py")),
        os.path.join("mailkit", "version.py"): _read(os.path.join(tmp, "mailkit", "version.py")),
    }


def _check_distractors_unchanged(workdir):
    seed = _seed_distractors()
    for rel, want in seed.items():
        path = os.path.join(workdir, rel)
        if not os.path.exists(path):
            return False, "distractor file removed: %s" % (rel,)
        if _read(path) != want:
            return False, ("distractor %s was modified; it must stay byte-identical "
                           "(its IDNA-looking line is dead/unrelated code)" % (rel,))
    return True, "distractors unchanged"


def _check_syntax(workdir):
    pkg = _pkg(workdir)
    for p in _py_files(pkg):
        try:
            ast.parse(_read(p))
        except SyntaxError as e:
            return False, "syntax error in %s: %s" % (os.path.relpath(p, workdir), e)
    return True, "syntax ok"


def verify(workdir):
    checks = [
        ("syntax", _check_syntax),
        ("consolidation", _check_consolidation),
        ("callers_use_helper", _check_callers_use_helper),
        ("bug_site", _check_bug_site),
        ("distractors_unchanged", _check_distractors_unchanged),
        ("behavior", _check_behavior),
    ]
    for name, fn in checks:
        ok, detail = fn(workdir)
        if not ok:
            return False, "[%s] %s" % (name, detail)
    return True, ("all checks passed: punycode helper introduced and every call site "
                  "(incl. the get_fqdn bug site) consolidated onto it")
