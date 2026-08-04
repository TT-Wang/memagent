"""Oracle for s8_router_50: exact final table + behavioral resolve() probes incl. redirect chains."""
import json
import os
import subprocess
import sys

EXPECTED_ROUTES = [{'path': '/', 'endpoint': 'home'}, {'path': '/docs', 'redirect_to': '/docs-v2'}, {'path': '/export', 'redirect_to': '/export-v2'}, {'path': '/billing', 'endpoint': 'billing_page'}, {'path': '/jobs', 'redirect_to': '/jobs-v2'}, {'path': '/help', 'redirect_to': '/help-v2'}, {'path': '/archive', 'redirect_to': '/archive-v2'}, {'path': '/jobs-v2', 'redirect_to': '/jobs-next'}, {'path': '/help-v2', 'endpoint': 'help_page_v3_v2'}, {'path': '/reports', 'endpoint': 'reports_page'}, {'path': '/jobs-next', 'endpoint': 'jobs_page'}, {'path': '/keys', 'endpoint': 'keys_page'}, {'path': '/files', 'redirect_to': '/files-v2'}, {'path': '/audit', 'endpoint': 'audit_page'}, {'path': '/labels', 'endpoint': 'labels_page'}, {'path': '/webhooks', 'endpoint': 'webhooks_page'}, {'path': '/plans', 'endpoint': 'plans_page'}, {'path': '/status', 'endpoint': 'status_page'}, {'path': '/archive-v2', 'endpoint': 'archive_page_v2'}, {'path': '/settings', 'endpoint': 'settings_page'}, {'path': '/runs', 'endpoint': 'runs_page'}, {'path': '/profile', 'endpoint': 'profile_page'}, {'path': '/invoices', 'endpoint': 'invoices_page'}, {'path': '/files-v2', 'endpoint': 'files_page_v3'}, {'path': '/digest', 'endpoint': 'digest_page'}]

PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from webr import router
paths = json.loads(sys.argv[2])
print(json.dumps({"ROUTES": router.ROUTES,
                  "resolved": {p: router.resolve(p) for p in paths}}))
'''


def _expected_resolve(path, depth=0):
    if depth > 5:
        return None
    for r in EXPECTED_ROUTES:
        if r["path"] == path:
            if "redirect_to" in r:
                return _expected_resolve(r["redirect_to"], depth + 1)
            return r["endpoint"]
    return None


def verify(workdir):
    fails = []
    probes = [r["path"] for r in EXPECTED_ROUTES] + ["/definitely-missing"]
    try:
        out = subprocess.run([sys.executable, "-c", PROBE, workdir, json.dumps(probes)],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return False, [f"import_or_run: {out.stderr.strip()[-300:]}"]
        got = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return False, [f"probe_crash: {type(exc).__name__}"]
    if got["ROUTES"] != EXPECTED_ROUTES:
        gp = {r["path"] for r in got["ROUTES"]}; ep = {r["path"] for r in EXPECTED_ROUTES}
        fails.append(f"table_mismatch missing={sorted(ep-gp)[:4]} extra={sorted(gp-ep)[:4]}")
    for p in probes:
        want = _expected_resolve(p)
        if got["resolved"].get(p) != want:
            fails.append(f"resolve({p})={got['resolved'].get(p)!r} want {want!r}")
            if len(fails) > 6:
                break
    return (not fails), fails
