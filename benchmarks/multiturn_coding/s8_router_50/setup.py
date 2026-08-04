import os

SEED = '"""Tiny URL router.\n\nROUTES is an ordered list of entries. Two entry shapes:\n  {"path": "/x", "endpoint": "handler_name"}            — a terminal route\n  {"path": "/old", "redirect_to": "/new"}               — a redirect (resolve() follows it)\n\nresolve(path) returns the endpoint name after following redirects (max 5 hops), or None.\nKeep entries EXACT — tests compare this table literally.\n"""\n\nROUTES = [\n    {"path": "/", "endpoint": "home"},\n    {"path": "/login", "endpoint": "auth_login"},\n    {"path": "/docs", "endpoint": "docs_index"},\n]\n\n\ndef resolve(path: str, _depth: int = 0):\n    if _depth > 5:\n        return None\n    for r in ROUTES:\n        if r["path"] == path:\n            if "redirect_to" in r:\n                return resolve(r["redirect_to"], _depth + 1)\n            return r["endpoint"]\n    return None\n'


def setup(workdir):
    pkg = os.path.join(workdir, 'webr')
    os.makedirs(pkg, exist_ok=True)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    with open(os.path.join(workdir, 'webr/router.py'), "w", encoding="utf-8") as f:
        f.write(SEED)
