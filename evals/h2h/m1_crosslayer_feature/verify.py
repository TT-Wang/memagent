"""Independent oracle for the ledger cross-layer tenant-threading task.

Checks BEHAVIOR with fresh inputs defined HERE (two tenants seeded inside the
verifier, cross-tenant requests the agent never saw), plus structural
invariants that prove the tenant datum is threaded AND used at every layer:
  (A) repository/service/controller signatures gained the tenant param and the
      serializer emits the tenant key;
  (B) behavior: in-tenant requests still work AND are stamped with the tenant;
      cross-tenant get/deposit/list are blocked at the repository layer (so a
      controller/service-only fix that forgets repository scoping FAILS);
  (C) distractors (store.py, auth.py, version.py) are unchanged.

verify() is NOT a file the agent is asked to touch, and the behavioral portion
imports the real package and drives it with requests built inside verify().
"""
import ast
import hashlib
import os
import re
import subprocess
import sys


# sha256 of the canonical seed bytes for each distractor file. These files must
# stay BYTE-IDENTICAL: any edit (incl. a blanket find/replace of tenant_id that
# brushes a distractor) flips the hash and fails verify. The seed is
# deterministic, so these hashes are stable across fresh workdirs.
_DISTRACTOR_HASHES = {
    "ledger/store.py": "9c3bf868462816be660d87bb14bb39968ba5edd18d262c8b75cb3fab27d71cf2",
    "ledger/auth.py": "860d15f4c131d9f16cf47a1080e8145e706f2847eb287715363429e86ef6c068",
    "ledger/version.py": "8422773c5ca3e7467dd1e03d362e9a7c726f212ea8aa7494e301f52c974dcab2",
}


def _pkg(workdir):
    return os.path.join(workdir, "ledger")


def _read(path):
    with open(path) as f:
        return f.read()


# ---------------------------------------------------------------------------
# Behavioral test: drive App.request(...) with TWO tenants seeded right here.
# ---------------------------------------------------------------------------
_BEHAVIOR = r'''
import sys, json
sys.path.insert(0, {workdir!r})

from ledger import App
from ledger.models import Account
from ledger.errors import NotFoundError

out = {{}}

app = App()
# Seed two tenants, including a SHARED account id "shared" owned by both at
# different rows is impossible (id is the store key), so we use distinct ids per
# tenant plus one id ("x9") that exists ONLY under tenant "globex" to test that
# tenant "acme" cannot reach it.
app.store.seed([
    Account(id="a1", tenant_id="acme",   owner="ann", balance=100),
    Account(id="a2", tenant_id="acme",   owner="amy", balance=10),
    Account(id="x9", tenant_id="globex", owner="gus", balance=500),
])

def cap(fn):
    try:
        return ("ok", fn())
    except NotFoundError as e:
        return ("notfound", str(e))
    except Exception as e:  # noqa: BLE001
        return ("error", type(e).__name__ + ": " + str(e))

# 1) in-tenant get works and is stamped with the tenant
out["acme_get_a1"] = cap(lambda: app.request(
    {{"tenant_id": "acme", "action": "get", "account_id": "a1"}}))

# 2) cross-tenant get is blocked: acme must NOT reach globex's x9
out["acme_get_x9"] = cap(lambda: app.request(
    {{"tenant_id": "acme", "action": "get", "account_id": "x9"}}))

# 3) cross-tenant deposit must be blocked AND must not mutate globex's balance
out["acme_deposit_x9"] = cap(lambda: app.request(
    {{"tenant_id": "acme", "action": "deposit", "account_id": "x9", "amount": 999}}))
# read globex's x9 as globex to confirm balance untouched (still 500)
out["globex_get_x9_after"] = cap(lambda: app.request(
    {{"tenant_id": "globex", "action": "get", "account_id": "x9"}}))

# 4) in-tenant deposit works
out["acme_deposit_a1"] = cap(lambda: app.request(
    {{"tenant_id": "acme", "action": "deposit", "account_id": "a1", "amount": 5}}))

# 5) list is tenant-scoped: acme sees only its 2 rows, never globex's x9
out["acme_list"] = cap(lambda: app.request(
    {{"tenant_id": "acme", "action": "list"}}))
out["globex_list"] = cap(lambda: app.request(
    {{"tenant_id": "globex", "action": "list"}}))

# 6) open stamps the new account's tenant, and a fresh tenant is isolated
out["zeta_open"] = cap(lambda: app.request(
    {{"tenant_id": "zeta", "action": "open", "account_id": "z1",
      "owner": "zoe", "balance": 7}}))
out["zeta_list"] = cap(lambda: app.request(
    {{"tenant_id": "zeta", "action": "list"}}))
# acme must NOT see zeta's freshly opened z1
out["acme_get_z1"] = cap(lambda: app.request(
    {{"tenant_id": "acme", "action": "get", "account_id": "z1"}}))

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


def _check_behavior(workdir):
    data, err = _run_behavior(workdir)
    if data is None:
        return False, err

    def status(key):
        v = data.get(key)
        return v[0] if isinstance(v, (list, tuple)) and v else None

    def value(key):
        v = data.get(key)
        return v[1] if isinstance(v, (list, tuple)) and len(v) == 2 else None

    # 1) in-tenant get works and carries the tenant under "tenant"
    if status("acme_get_a1") != "ok":
        return False, "in-tenant get a1 failed: %r" % (data.get("acme_get_a1"),)
    resp = value("acme_get_a1")
    if not isinstance(resp, dict):
        return False, "get a1 did not return a dict: %r" % (resp,)
    if resp.get("tenant") != "acme":
        return False, ("serializer must stamp the response with the tenant under "
                       "'tenant' (expected 'acme', got %r in %r)"
                       % (resp.get("tenant"), resp))
    for k in ("id", "owner", "balance"):
        if k not in resp:
            return False, "serializer dropped original key %r: %r" % (k, resp)
    if resp.get("id") != "a1" or resp.get("owner") != "ann":
        return False, "serializer corrupted original fields: %r" % (resp,)

    # 2) cross-tenant get blocked (repository scoping). acme must NOT reach x9.
    if status("acme_get_x9") != "notfound":
        return False, ("cross-tenant get must raise NotFoundError (repository must "
                       "scope by tenant); got %r -- a controller/service-only fix "
                       "that forgets repository scoping leaks here"
                       % (data.get("acme_get_x9"),))

    # 3) cross-tenant deposit blocked AND globex's balance untouched.
    if status("acme_deposit_x9") != "notfound":
        return False, ("cross-tenant deposit must raise NotFoundError; got %r"
                       % (data.get("acme_deposit_x9"),))
    gx = value("globex_get_x9_after")
    if status("globex_get_x9_after") != "ok" or not isinstance(gx, dict):
        return False, "could not read globex x9 after cross-tenant deposit: %r" % (data.get("globex_get_x9_after"),)
    if gx.get("balance") != 500:
        return False, ("cross-tenant deposit mutated another tenant's balance "
                       "(globex x9 should still be 500, got %r) -- repository did "
                       "not scope the write" % (gx.get("balance"),))
    if gx.get("tenant") != "globex":
        return False, "globex x9 response not stamped with its tenant: %r" % (gx,)

    # 4) in-tenant deposit works and updates balance (100 + 5 = 105).
    if status("acme_deposit_a1") != "ok":
        return False, "in-tenant deposit a1 failed: %r" % (data.get("acme_deposit_a1"),)
    dep = value("acme_deposit_a1")
    if not isinstance(dep, dict) or dep.get("balance") != 105:
        return False, "in-tenant deposit did not update balance to 105: %r" % (dep,)
    if dep.get("tenant") != "acme":
        return False, "deposit response not stamped with tenant: %r" % (dep,)

    # 5) list is tenant-scoped.
    al = value("acme_list")
    if status("acme_list") != "ok" or not isinstance(al, dict):
        return False, "acme list failed: %r" % (data.get("acme_list"),)
    if al.get("count") != 2:
        return False, ("acme list must contain exactly its 2 accounts, got count=%r "
                       "items=%r -- list is not tenant-scoped"
                       % (al.get("count"), al.get("items")))
    item_tenants = {it.get("tenant") for it in (al.get("items") or [])}
    if item_tenants != {"acme"}:
        return False, ("acme list leaked other tenants' rows (tenants seen: %r)"
                       % (item_tenants,))
    item_ids = sorted(it.get("id") for it in (al.get("items") or []))
    if item_ids != ["a1", "a2"]:
        return False, "acme list returned wrong account ids: %r" % (item_ids,)
    gl = value("globex_list")
    if status("globex_list") != "ok" or not isinstance(gl, dict) or gl.get("count") != 1:
        return False, "globex list must contain exactly 1 account, got %r" % (gl,)
    if {it.get("id") for it in (gl.get("items") or [])} != {"x9"}:
        return False, "globex list returned wrong rows: %r" % (gl.get("items"),)

    # 6) open stamps tenant; new account isolated to its tenant.
    if status("zeta_open") != "ok":
        return False, "open for tenant zeta failed: %r" % (data.get("zeta_open"),)
    zo = value("zeta_open")
    if not isinstance(zo, dict) or zo.get("tenant") != "zeta":
        return False, ("open must stamp the new account with its tenant "
                       "(expected 'zeta', got %r) -- repository.create dropped the "
                       "tenant" % (zo,))
    zl = value("zeta_list")
    if status("zeta_list") != "ok" or not isinstance(zl, dict) or zl.get("count") != 1:
        return False, "zeta list must contain exactly its 1 newly opened account: %r" % (zl,)
    if status("acme_get_z1") != "notfound":
        return False, ("acme must NOT see zeta's freshly opened account z1; got %r"
                       % (data.get("acme_get_z1"),))

    return True, "behavior OK"


# ---------------------------------------------------------------------------
# Structural checks: new tenant param threaded at each layer.
# ---------------------------------------------------------------------------
def _check_layers_threaded(workdir):
    pkg = _pkg(workdir)

    # Repository: each public read/write must take a tenant_id param.
    repo = _read(os.path.join(pkg, "repository.py"))
    repo_sigs = {
        "get": re.compile(r"\bdef\s+get\s*\(\s*self\s*,\s*tenant_id\s*,"),
        "list": re.compile(r"\bdef\s+list\s*\(\s*self\s*,\s*tenant_id\s*\)"),
        "create": re.compile(r"\bdef\s+create\s*\(\s*self\s*,\s*tenant_id\s*,"),
    }
    for name, rx in repo_sigs.items():
        if not rx.search(repo):
            return False, "repository.%s must take tenant_id as its first arg" % (name,)
    # repository must actually USE tenant_id to scope (not just accept it).
    if "tenant_id" not in repo or "row.tenant_id" not in repo and ".tenant_id" not in repo:
        return False, "repository must compare row.tenant_id against tenant_id to scope reads"

    # Service: every public method must take a leading tenant_id and forward it.
    svc = _read(os.path.join(pkg, "service.py"))
    svc_sigs = {
        "open_account": re.compile(r"\bdef\s+open_account\s*\(\s*self\s*,\s*tenant_id\s*,"),
        "get_account": re.compile(r"\bdef\s+get_account\s*\(\s*self\s*,\s*tenant_id\s*,"),
        "list_accounts": re.compile(r"\bdef\s+list_accounts\s*\(\s*self\s*,\s*tenant_id\s*\)"),
        "deposit": re.compile(r"\bdef\s+deposit\s*\(\s*self\s*,\s*tenant_id\s*,"),
    }
    for name, rx in svc_sigs.items():
        if not rx.search(svc):
            return False, "service.%s must take tenant_id as its first arg" % (name,)
    if not re.search(r"self\.repository\.get\s*\(\s*tenant_id\s*,", svc):
        return False, "service must forward tenant_id into repository.get(tenant_id, ...)"
    if not re.search(r"self\.repository\.list\s*\(\s*tenant_id\s*\)", svc):
        return False, "service.list_accounts must call repository.list(tenant_id)"

    # Controller: must read request["tenant_id"] and pass it into service calls.
    ctl = _read(os.path.join(pkg, "controller.py"))
    if not re.search(r"request\s*\[\s*[\"']tenant_id[\"']\s*\]", ctl):
        return False, "controller.handle must read request['tenant_id']"
    # every service.<method>( call in the controller must pass tenant_id first.
    bad = []
    for m in re.finditer(r"self\.service\.(\w+)\s*\(([^)]*)", ctl):
        method, args = m.group(1), m.group(2).strip()
        first = args.split(",")[0].strip()
        if first != "tenant_id":
            bad.append("%s(%s...)" % (method, first))
    if bad:
        return False, ("controller must pass tenant_id as the first arg to every "
                       "service call; offenders: %s" % (", ".join(bad),))

    # Serializer: must emit the tenant under the "tenant" key.
    ser = _read(os.path.join(pkg, "serializer.py"))
    if not re.search(r"[\"']tenant[\"']\s*:\s*\w+\.tenant_id", ser):
        return False, "serialize_account must include 'tenant': account.tenant_id"

    return True, "layers threaded"


def _check_distractors_unchanged(workdir):
    """store.py, auth.py, version.py are distractors that must stay
    BYTE-IDENTICAL to the seed. Each shares the tenant_id substring (or looks
    layer-adjacent) so a blanket 'thread tenant_id everywhere' / find-replace
    edit would brush them; the sha256 guard fails the moment one byte changes."""
    for rel, expected in _DISTRACTOR_HASHES.items():
        path = os.path.join(workdir, rel)
        if not os.path.exists(path):
            return False, "distractor file removed: %s" % (rel,)
        with open(path, "rb") as f:
            got = hashlib.sha256(f.read()).hexdigest()
        if got != expected:
            return False, ("distractor %s was modified (must stay byte-identical "
                           "to the seed). Its local 'tenant_id' is unrelated to the "
                           "request pipeline and must NOT be rewired." % (rel,))
    return True, "distractors unchanged"


def _check_syntax(workdir):
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
        ("layers_threaded", _check_layers_threaded),
        ("distractors_unchanged", _check_distractors_unchanged),
        ("behavior", _check_behavior),
    ]
    for name, fn in checks:
        ok, detail = fn(workdir)
        if not ok:
            return False, "[%s] %s" % (name, detail)
    return True, "all checks passed: tenant_id threaded + used across repository, service, controller, serializer"
