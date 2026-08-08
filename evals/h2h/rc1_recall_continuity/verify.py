"""Independent behavioral oracle for the multi-turn recall/continuity scenario.

This file is NOT given to the benchmarked agent to edit. It imports the agent's
final ``appconf`` package in a FRESH subprocess and checks that the FINAL turn's
new configuration options honor the THREE durable house conventions established
back in turn 1 -- and never restated afterwards:

    (1) every config key is UPPER_SNAKE with the ``APP_`` prefix,
    (2) rendered config uses a 2-space indent unit,
    (3) the default service port is 8137.

The oracle does NOT trust any prompt example verbatim. It:
  * inspects the LIVE registry after a bare ``import appconf`` (so registration
    must really happen at import time, not be faked),
  * renders the registry and asserts the NEW keys (chosen by the agent) carry
    the APP_ prefix and land under the ``[app]`` section indented by 2 spaces,
  * exercises the renderer on a FRESH, agent-unseen registry to prove the
    2-space indent unit is intact regardless of the specific keys,
  * drives the two consumer stubs (server port, client timeout) behaviorally,
  * runs REGRESSION GUARDS for the turn 2-6 edits and the seed tests.

It runs in a subprocess so a crashing/looping import cannot take down the
parent and so import caching never masks a broken module. Stale bytecode is
dropped first; the child runs with -B (no .pyc writes).
"""
import os
import sys
import subprocess

PY = sys.executable


PROBE = r'''
import sys, io
sys.path.insert(0, ".")

fails = []
def check(cond, tag):
    if not cond:
        fails.append(tag)

# Fresh import: registration must happen at import time.
import appconf
from appconf.schema import Option, REGISTRY, get_option, register, kinds, option_count
from appconf.render import render_config, INDENT, _section_of

# ----- recall (3): the service port default is 8137 ----------------------
port = get_option("APP_PORT")
check(port is not None, "port_registered")
if port is not None:
    check(int(port.default) == 8137, "port_default_8137")
    check(port.kind == "int", "port_kind_int")

# server consumer must return the recalled port behaviorally
from appconf.server import bind_address
try:
    host, p = bind_address("127.0.0.1")
    check(p == 8137, "server_binds_8137")
except Exception as e:
    fails.append("server_bind_raised:%s" % type(e).__name__)

# ----- recall (1): the NEW keys honor UPPER_SNAKE + APP_ prefix -----------
# Identify the two new options by their semantics (default + help), NOT by a
# name the prompt dictated -- the agent chose the names, and choosing them per
# the APP_ convention is exactly the recalled fact under test.
def find_new(default, help_substr):
    cand = []
    for o in REGISTRY:
        h = (o.help or "").lower()
        if o.default == default and o.kind == "int" and help_substr in h:
            cand.append(o)
    return cand

timeout_cands = find_new(30, "timeout")
retry_cands = find_new(5, "retr")
check(len(timeout_cands) == 1, "timeout_option_present")
check(len(retry_cands) == 1, "retries_option_present")

def is_app_upper_snake(name):
    if not name.startswith("APP_"):
        return False
    if name != name.upper():
        return False
    # UPPER_SNAKE: only A-Z, 0-9 and underscores, no leading/trailing/double __
    import re
    if not re.fullmatch(r"APP_[A-Z0-9]+(?:_[A-Z0-9]+)*", name):
        return False
    return True

new_names = []
if timeout_cands:
    new_names.append(timeout_cands[0].name)
if retry_cands:
    new_names.append(retry_cands[0].name)
for nm in new_names:
    check(is_app_upper_snake(nm), "new_key_prefix_convention:%s" % nm)

# The new keys must also be DISTINCT from the port and from each other.
check(len(set(new_names)) == len(new_names), "new_keys_distinct")
check("APP_PORT" not in new_names, "new_keys_not_port")

# ----- recall (2)+(1): the new keys render under [app] with 2-space indent
out = render_config()
out_lines = out.splitlines()

# INDENT must be the 2-space house unit.
check(INDENT == 2, "indent_unit_is_2")

# Each new key must appear on a line indented by EXACTLY two spaces and must be
# grouped under the [app] section header.
def line_for(name):
    for ln in out_lines:
        if name in ln and "=" in ln:
            return ln
    return None

def section_of_key(name):
    cur = None
    for ln in out_lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1]
        elif name in ln and "=" in ln:
            return cur
    return None

for nm in new_names:
    ln = line_for(nm)
    check(ln is not None, "new_key_rendered:%s" % nm)
    if ln is not None:
        leading = len(ln) - len(ln.lstrip(" "))
        check(leading == 2, "new_key_2space_indent:%s(%d)" % (nm, leading))
        check("\t" not in ln, "new_key_no_tab:%s" % nm)
    check(section_of_key(nm) == "app", "new_key_in_app_section:%s" % nm)

# APP_PORT itself still renders under [app] indented by 2.
pln = line_for("APP_PORT")
check(pln is not None and (len(pln) - len(pln.lstrip(" "))) == 2, "port_2space_indent")
check(section_of_key("APP_PORT") == "app", "port_in_app_section")

# ----- behavioral: client consumer returns the recalled timeout ----------
from appconf.client import timeout_seconds
try:
    t = timeout_seconds()
    check(t == 30 and isinstance(t, int), "client_timeout_30")
except Exception as e:
    fails.append("client_timeout_raised:%s" % type(e).__name__)

# ----- INDEPENDENCE: render an agent-UNSEEN registry; the 2-space indent ---
# unit and APP_-section derivation must hold for keys that never appeared in any
# prompt, proving the renderer wasn't special-cased to the example.
fresh = [
    Option("APP_ZZQUUX", 7, "int", "unseen knob"),
    Option("APP_DB_POOL", 12, "int", "unseen pool size"),
]
fresh_out = render_config(fresh)
fl = fresh_out.splitlines()
# both unseen keys indented by exactly 2 and under [app]
def fresh_section(name):
    cur = None
    for ln in fl:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1]
        elif name in ln and "=" in ln:
            return cur, len(ln) - len(ln.lstrip(" "))
    return None, None
for nm in ("APP_ZZQUUX", "APP_DB_POOL"):
    sec, ind = fresh_section(nm)
    check(sec == "app", "unseen_%s_app_section" % nm)
    check(ind == 2, "unseen_%s_2space" % nm)
# section derivation for a non-APP, underscored name still works generically
check(_section_of("DB_HOST") == "db", "section_derivation_generic")
check(_section_of("solo") == "default", "section_derivation_default")

# ----- REGRESSION GUARD: turn 3 single-source-of-truth kinds() -----------
check(set(kinds()) == {"int", "str", "bool"}, "kinds_set")
try:
    Option("APP_BAD", 1, "weird")
    fails.append("kind_validation_lost")
except ValueError:
    pass

# ----- REGRESSION GUARD: turn 5 option_count exported + correct ----------
check(callable(getattr(appconf, "option_count", None)), "option_count_exported")
check("option_count" in getattr(appconf, "__all__", []), "option_count_in_all")
check(option_count() == len(REGISTRY), "option_count_value")
# at least the 3 defaults are present (port + timeout + retries)
check(option_count() >= 3, "registry_has_three_defaults")

# ----- REGRESSION GUARD: turn 2 docstring documents blank-line behavior ---
import appconf.render as rmod
rdoc = (rmod.__doc__ or "").lower()
check("blank line" in rdoc, "render_docstring_blank_line")

# ----- REGRESSION GUARD: turn 4 cli --help short-circuits -----------------
from appconf.cli import main as cli_main
buf = io.StringIO()
old = sys.stdout
sys.stdout = buf
try:
    rc = cli_main(["--help"])
finally:
    sys.stdout = old
help_out = buf.getvalue()
check(rc == 0, "cli_help_rc0")
check("usage" in help_out.lower(), "cli_help_usage")
check("[app]" not in help_out, "cli_help_no_render")
# normal invocation still renders
buf2 = io.StringIO()
sys.stdout = buf2
try:
    cli_main([])
finally:
    sys.stdout = old
check("[app]" in buf2.getvalue(), "cli_normal_renders")

if fails:
    print("FAILS:" + ",".join(fails))
    sys.exit(1)
print("ALL_OK")
sys.exit(0)
'''


def _drop_pyc(workdir):
    for root, _dirs, files in os.walk(workdir):
        if os.path.basename(root) == "__pycache__":
            for fn in files:
                if fn.endswith(".pyc"):
                    try:
                        os.remove(os.path.join(root, fn))
                    except OSError:
                        pass


def _run_seed_tests(workdir):
    """Regression guard: the seed smoke tests must still pass."""
    test = os.path.join(workdir, "tests", "test_seed.py")
    if not os.path.isfile(test):
        return (False, "tests/test_seed.py is missing")
    env = dict(os.environ)
    env["PYTHONPATH"] = workdir + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [PY, "-B", test],
            cwd=workdir, capture_output=True, text=True, timeout=60, env=env,
        )
    except subprocess.TimeoutExpired:
        return (False, "seed tests timed out")
    if proc.returncode != 0:
        out = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
        return (False, "seed tests failed:\n" + "\n".join(out[-12:]))
    return (True, "")


def verify(workdir):
    pkg = os.path.join(workdir, "appconf")
    if not os.path.isfile(os.path.join(pkg, "__init__.py")):
        return (False, "appconf package is missing")

    _drop_pyc(workdir)

    # Main behavioral probe.
    try:
        proc = subprocess.run(
            [PY, "-B", "-c", PROBE],
            cwd=workdir, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return (False, "probe timed out (possible infinite loop on import/render)")
    except Exception as e:  # noqa: BLE001
        return (False, "probe failed to launch: %s" % e)

    out = (proc.stdout or "") + (proc.stderr or "")
    if not (proc.returncode == 0 and "ALL_OK" in out):
        tail = "\n".join(out.strip().splitlines()[-18:]) if out.strip() else "(no output)"
        return (False, "probe failed:\n" + tail)

    # Seed-tests regression guard (separate subprocess).
    ok, detail = _run_seed_tests(workdir)
    if not ok:
        return (False, detail)

    return (True, "all recall/continuity + regression checks passed "
                  "(turn-1 conventions honored on new turn-7 options)")
