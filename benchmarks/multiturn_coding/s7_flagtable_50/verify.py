"""Oracle for s7_flagtable_50: exact final registry state (rendered from the generator replay).

Subprocess import (never in-process), exact dict comparison. A single missed/mis-applied turn
anywhere in the 50 shows up as a diff here.
"""
import json
import subprocess
import sys

EXPECTED_FLAGS = {'avatars-next': {'default': True, 'owner': 'web'}, 'billing-next-next-v2': {'default': False, 'owner': 'payments'}, 'dashboards': {'default': False, 'owner': 'web'}, 'digest': {'default': False, 'owner': 'growth'}, 'exports': {'default': False, 'owner': 'infra'}, 'labels': {'default': False, 'owner': 'search'}, 'new-checkout': {'default': False, 'owner': 'infra'}, 'previews': {'default': True, 'owner': 'mobile'}, 'themes': {'default': False, 'owner': 'data'}, 'webhooks': {'default': False, 'owner': 'search'}}
EXPECTED_RETIRED = {'beta-search': {'default': False, 'owner': 'platform'}, 'dark-mode': {'default': False, 'owner': 'web'}, 'imports-v2': {'default': True, 'owner': 'mobile'}, 'invites': {'default': False, 'owner': 'growth'}}

PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from flags import registry
print(json.dumps({"FLAGS": registry.FLAGS, "RETIRED": registry.RETIRED,
                  "en": registry.is_enabled(sys.argv[2])}))
'''


def verify(workdir):
    fails = []
    probe_flag = sorted(EXPECTED_FLAGS)[0] if EXPECTED_FLAGS else "dark-mode"
    try:
        out = subprocess.run([sys.executable, "-c", PROBE, workdir, probe_flag],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return False, [f"import_or_run: {out.stderr.strip()[-300:]}"]
        got = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return False, [f"probe_crash: {type(exc).__name__}"]
    if got["FLAGS"] != EXPECTED_FLAGS:
        missing = sorted(set(EXPECTED_FLAGS) - set(got["FLAGS"]))
        extra = sorted(set(got["FLAGS"]) - set(EXPECTED_FLAGS))
        wrong = sorted(k for k in set(got["FLAGS"]) & set(EXPECTED_FLAGS)
                       if got["FLAGS"][k] != EXPECTED_FLAGS[k])
        fails.append(f"flags_mismatch missing={missing[:4]} extra={extra[:4]} wrong={wrong[:4]}")
    if got["RETIRED"] != EXPECTED_RETIRED:
        fails.append("retired_mismatch")
    if got["en"] != EXPECTED_FLAGS.get(probe_flag, {}).get("default"):
        fails.append("is_enabled_broken")
    return (not fails), fails
