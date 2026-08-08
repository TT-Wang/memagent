import os
import sys
import json
import subprocess


# Independent oracle. We do NOT trust any test the agent can see and we do NOT
# import the agent's files into THIS process. We import the (possibly-edited)
# library in a FRESH subprocess and:
#
#   1. dispatch the new "status" route with an UNSEEN request payload and check
#      it returns the status string "ok" through the router (so the route was
#      registered the project's way and survives the router's validation);
#   2. independently inspect the raw ROUTES["status"] entry to confirm it has
#      the exact magic protocol rev (0xC0FFEE) and the mandated 3-tuple shape;
#   3. confirm the handler's response envelope carries that same magic rev.
#
# All three facts (the 0xC0FFEE constant, the (handler, rev, name) tuple form,
# and the {"rev","endpoint","body"} envelope) live ONLY in the SKILL.md body, so
# a no-op workdir — or an agent that never loaded the skill — cannot satisfy
# them. The magic rev is independently recomputed here (NOT imported from the
# agent's possibly-tampered protocol.py) so the check is anchored to 0xC0FFEE.

_MAGIC_REV = 0xC0FFEE

_CHILD = r'''
import json, sys

result = {}
try:
    import router
    import routes
    import protocol

    # (1) dispatch the new route with an UNSEEN request payload.
    resp = router.dispatch("status", {"unseen": "x-7f3a", "n": 42})
    result["dispatch_ok"] = True
    result["resp"] = resp if isinstance(resp, dict) else {"_repr": repr(resp)}

    # (2) raw ROUTES entry shape + stamped rev.
    entry = routes.ROUTES.get("status")
    result["entry_present"] = entry is not None
    if isinstance(entry, tuple):
        result["entry_len"] = len(entry)
        if len(entry) == 3:
            handler, rev, name = entry
            result["entry_handler_callable"] = callable(handler)
            result["entry_rev"] = rev
            result["entry_name"] = name
    else:
        result["entry_len"] = -1

    # (3) what the project thinks its current rev is (must equal the magic value).
    result["protocol_rev"] = getattr(protocol, "PROTOCOL_REV", None)

except BaseException as e:
    result["error"] = "%s: %s" % (type(e).__name__, e)

sys.stdout.write(json.dumps(result))
'''


def verify(workdir):
    for fn in ("router.py", "routes.py", "protocol.py"):
        if not os.path.isfile(os.path.join(workdir, fn)):
            return False, "%s not found in workdir" % fn

    # Drop stale bytecode so we always test the CURRENT source.
    pycache = os.path.join(workdir, "__pycache__")
    if os.path.isdir(pycache):
        for fn in os.listdir(pycache):
            if fn.endswith(".pyc"):
                try:
                    os.remove(os.path.join(pycache, fn))
                except OSError:
                    pass

    proc = subprocess.run(
        [sys.executable, "-B", "-c", _CHILD],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        return False, "child crashed (rc=%d): %s" % (
            proc.returncode, (proc.stderr or proc.stdout)[-400:])
    try:
        r = json.loads(proc.stdout)
    except Exception as e:
        return False, "could not parse child output: %s :: %r" % (e, proc.stdout[-300:])

    if "error" in r:
        return False, "router rejected the new route: " + str(r["error"])[:200]

    # --- The route must dispatch and return "ok" through the router.
    if not r.get("dispatch_ok"):
        return False, "dispatch('status', ...) did not complete"
    resp = r.get("resp") or {}
    if resp.get("endpoint") != "status":
        return False, "envelope 'endpoint' is %r, expected 'status'" % (resp.get("endpoint"),)
    if resp.get("body") != "ok":
        return False, "status handler body is %r, expected 'ok'" % (resp.get("body"),)

    # --- The MAGIC constant must be present, exactly, in the response envelope
    #     (anchored to 0xC0FFEE, independent of the agent's protocol.py).
    if resp.get("rev") != _MAGIC_REV:
        return False, ("response 'rev' is %r, expected the magic PROTOCOL_REV "
                       "0xC0FFEE (%d) — only the skill states this value"
                       % (resp.get("rev"), _MAGIC_REV))

    # --- The raw ROUTES entry must be the mandated 3-tuple, stamped with the
    #     magic rev, with the matching name (the non-guessable registration form).
    if not r.get("entry_present"):
        return False, "ROUTES has no 'status' entry"
    if r.get("entry_len") != 3:
        return False, ("ROUTES['status'] is not a 3-tuple (got len=%r); the skill "
                       "mandates (handler, PROTOCOL_REV, name)" % (r.get("entry_len"),))
    if not r.get("entry_handler_callable"):
        return False, "ROUTES['status'][0] is not a callable handler"
    if r.get("entry_rev") != _MAGIC_REV:
        return False, ("ROUTES['status'][1] is %r, expected the magic PROTOCOL_REV "
                       "0xC0FFEE (%d)" % (r.get("entry_rev"), _MAGIC_REV))
    if r.get("entry_name") != "status":
        return False, "ROUTES['status'][2] is %r, expected 'status'" % (r.get("entry_name"),)

    # --- Sanity: the project's own PROTOCOL_REV must still be the magic value
    #     (guards against the agent editing protocol.py to something else).
    if r.get("protocol_rev") != _MAGIC_REV:
        return False, ("protocol.PROTOCOL_REV is %r, expected 0xC0FFEE (%d)"
                       % (r.get("protocol_rev"), _MAGIC_REV))

    return True, ("status endpoint dispatches to 'ok' through the router with the "
                  "exact magic PROTOCOL_REV 0xC0FFEE and the mandated "
                  "(handler, rev, name) ROUTES form + envelope from the skill")
