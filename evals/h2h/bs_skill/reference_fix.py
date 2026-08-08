import os


# Reference fix: do exactly what an agent that LOADED the skill would do —
# create the `status` endpoint module per the mandated handler/envelope shape and
# register it in routes.py with the exact (handler, PROTOCOL_REV, name) 3-tuple.
# The magic constant comes in by importing PROTOCOL_REV by name (its concrete
# value 0xC0FFEE is what the skill teaches and what the router enforces). We do
# NOT touch router.py or protocol.py.

_STATUS_ENDPOINT = '''\
"""endpoints/status.py — the 'status' endpoint (added per the project skill)."""
from protocol import PROTOCOL_REV


def status(request):
    return {"rev": PROTOCOL_REV, "endpoint": "status", "body": "ok"}
'''

_IMPORT_LINE = "from endpoints.status import status\n"
_REGISTER_LINE = '    "status": (status, PROTOCOL_REV, "status"),\n'


def apply(workdir):
    # 1) write the endpoint module.
    ep_dir = os.path.join(workdir, "endpoints")
    os.makedirs(ep_dir, exist_ok=True)
    with open(os.path.join(ep_dir, "status.py"), "w", encoding="utf-8") as f:
        f.write(_STATUS_ENDPOINT)

    # 2) register it in routes.py: add the import after the ping import and the
    #    entry inside the ROUTES dict, in the mandated 3-tuple form.
    routes_path = os.path.join(workdir, "routes.py")
    with open(routes_path, "r", encoding="utf-8") as f:
        text = f.read()

    if _IMPORT_LINE not in text:
        anchor = "from endpoints.ping import ping\n"
        if anchor in text:
            text = text.replace(anchor, anchor + _IMPORT_LINE, 1)
        else:
            text = _IMPORT_LINE + text

    if '"status":' not in text:
        # insert the entry right after the ping entry line.
        ping_entry = '    "ping": (ping, PROTOCOL_REV, "ping"),\n'
        if ping_entry in text:
            text = text.replace(ping_entry, ping_entry + _REGISTER_LINE, 1)
        else:
            # fallback: insert before the closing brace of the ROUTES dict.
            idx = text.rfind("}")
            text = text[:idx] + _REGISTER_LINE + text[idx:]

    with open(routes_path, "w", encoding="utf-8") as f:
        f.write(text)
