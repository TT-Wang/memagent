"""Independent oracle for the wf1 WIDE cross-file rename scenario.

Two independent gates, both must pass:

  (A) GREP / STRUCTURAL: the OLD name `rollout_percentage` must be GONE from
      every .py file in the package AND from flags.json, and the NEW name
      `rollout_pct` must actually be USED (as a field, a parameter, and a JSON
      key). The distractor names `bucket_percentage` and `sample_percentage`
      must STILL be present and untouched.

  (B) BEHAVIORAL: run the agent's (possibly-edited) package END-TO-END in a
      FRESH subprocess on INPUTS THE AGENT NEVER SAW -- brand-new flag names,
      users, and a config the verifier writes itself using the NEW key -- and
      assert real enrollment decisions against a ground truth the oracle
      recomputes ITSELF (its own sha256 bucketing), so the agent cannot pass by
      hard-coding the prompt's example or stubbing the app layer. Includes
      REGRESSION GUARDS: allowlist / disabled / 0% / 100% semantics, JSON
      round-trip under the new key, and the override kwarg path.

Robust: a child crash -> (False, detail), never an exception. Stale .pyc are
dropped before importing.
"""
import hashlib
import json
import os
import subprocess
import sys


_OLD = "rollout_percentage"
_NEW = "rollout_pct"


def _pkg(workdir):
    return os.path.join(workdir, "rollout")


def _iter_py(workdir):
    for root, _dirs, files in os.walk(workdir):
        if os.path.basename(root) == "__pycache__":
            continue
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(root, fn)


def _read(path):
    with open(path, "r") as f:
        return f.read()


def _drop_pyc(workdir):
    """Drop cached bytecode so we always import the CURRENT source."""
    for root, dirs, _files in os.walk(workdir):
        if os.path.basename(root) == "__pycache__":
            for fn in os.listdir(root):
                if fn.endswith(".pyc"):
                    try:
                        os.remove(os.path.join(root, fn))
                    except OSError:
                        pass


# ---------------------------------------------------------------------------
# (A) Structural / grep gate.
# ---------------------------------------------------------------------------
def _check_structural(workdir):
    pkg = _pkg(workdir)
    if not os.path.isdir(pkg):
        return False, "rollout/ package not found in workdir"

    # A1: NO occurrence of the old name in ANY .py file.
    offenders = []
    new_seen_in_py = False
    for path in _iter_py(workdir):
        src = _read(path)
        rel = os.path.relpath(path, workdir)
        if _OLD in src:
            offenders.append(rel)
        if _NEW in src:
            new_seen_in_py = True
    if offenders:
        return False, ("old name %r still present in .py file(s): %s"
                       % (_OLD, ", ".join(sorted(set(offenders)))))

    # A2: the new name must actually be used somewhere in the .py sources.
    if not new_seen_in_py:
        return False, "new name %r is not used in any .py file" % (_NEW,)

    # A3: the new name must be a real dataclass FIELD on FeatureFlag and a real
    # parameter, not just a comment. Cheap concrete anchors:
    model = _read(os.path.join(pkg, "model.py"))
    if (_NEW + ":") not in model and (_NEW + " ") not in model:
        return False, "model.py does not declare %r as a FeatureFlag field" % (_NEW,)
    if _OLD in model:
        return False, "model.py still references the old name"

    # A4: flags.json must move to the new key (and not keep the old one).
    flags_json = os.path.join(workdir, "flags.json")
    if not os.path.isfile(flags_json):
        return False, "flags.json missing"
    fj = _read(flags_json)
    if _OLD in fj:
        return False, "flags.json still uses the old key %r" % (_OLD,)
    if _NEW not in fj:
        return False, "flags.json does not use the new key %r" % (_NEW,)
    # it must still be valid JSON with the rollout key on each flag
    try:
        cfg = json.loads(fj)
    except Exception as e:  # noqa: BLE001
        return False, "flags.json is not valid JSON: %s" % (e,)
    for item in cfg.get("flags", []):
        if _NEW not in item:
            return False, "a flag entry in flags.json lacks the %r key: %r" % (_NEW, item)
        if _OLD in item:
            return False, "a flag entry in flags.json still has the %r key" % (_OLD,)

    # A5: distractors must SURVIVE untouched (names still present somewhere).
    all_py = "\n".join(_read(p) for p in _iter_py(workdir))
    if "bucket_percentage" not in all_py:
        return False, "distractor name 'bucket_percentage' was wrongly removed/renamed"
    if "sample_percentage" not in all_py:
        return False, "distractor name 'sample_percentage' was wrongly removed/renamed"
    # And the distractors must not have been corrupted into the new token.
    if "bucket_rollout_pct" in all_py or "sample_rollout_pct" in all_py:
        return False, "a distractor name was corrupted by an over-broad rename"

    return True, "structural ok"


# ---------------------------------------------------------------------------
# (B) Behavioral gate: fresh subprocess, unseen inputs, oracle-computed truth.
# ---------------------------------------------------------------------------

# The child uses the NEW public field/key name (rollout_pct). If the rename is
# incomplete, constructing a FeatureFlag with rollout_pct=... or loading a
# config keyed by rollout_pct will fail -> child error -> (False, ...).
_CHILD = r'''
import json, sys
sys.path.insert(0, {workdir!r})

from rollout import FeatureFlag, FlagService, RolloutEngine
from rollout.config import load_flags, dump_flags

out = {{}}

# --- 1) Construct flags via the NEW field name on UNSEEN flag names. -------
f_mid = FeatureFlag(name="oracle_feature", rollout_pct=30, allowlist=["always_in"])
f_off = FeatureFlag(name="oracle_off", rollout_pct=60, enabled=False)
f_zero = FeatureFlag(name="oracle_zero", rollout_pct=0, allowlist=["vip_zero"])
f_full = FeatureFlag(name="oracle_full", rollout_pct=100)
flags = {{f.name: f for f in [f_mid, f_off, f_zero, f_full]}}

# to_dict/from_dict must round-trip under the new key.
d = f_mid.to_dict()
out["to_dict_key_new"] = ("rollout_pct" in d) and ("rollout_percentage" not in d)
out["to_dict_value"] = d.get("rollout_pct")
rebuilt = FeatureFlag.from_dict(d)
out["from_dict_roundtrip"] = (rebuilt.rollout_pct == 30 and rebuilt.name == "oracle_feature")

# --- 2) Drive the engine on UNSEEN users. ---------------------------------
eng = RolloutEngine(flags)
users = ["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi", "always_in"]
out["mid_enrolled"] = eng.enrolled_users("oracle_feature", users)
out["off_enrolled"] = eng.enrolled_users("oracle_off", users)
out["zero_enrolled"] = eng.enrolled_users("oracle_zero", users + ["vip_zero"])
out["full_enrolled"] = eng.enrolled_users("oracle_full", users)

# --- 3) JSON round-trip through disk under the NEW key. --------------------
cfg_path = sys.argv[1]
dump_flags(flags, cfg_path)
with open(cfg_path) as fh:
    raw = fh.read()
out["disk_has_new_key"] = ("rollout_pct" in raw)
out["disk_has_old_key"] = ("rollout_percentage" in raw)
reloaded = load_flags(cfg_path)
out["reload_value"] = reloaded["oracle_feature"].rollout_pct

# --- 4) Service facade + override kwarg path (NEW kwarg name). -------------
svc = FlagService(flags)
out["svc_mid_enrolled"] = svc.enrolled_users("oracle_feature", users)
svc.override_flag("oracle_zero", rollout_pct=100)
out["after_override_zero"] = svc.enrolled_users("oracle_zero", users)
# distractor knob must still exist on the service untouched
out["sample_pct_attr"] = getattr(svc, "sample_percentage", "MISSING")

print("JSON_START")
print(json.dumps(out, default=str))
'''


def _oracle_bucket(flag_name, user_id):
    """Independently recompute the deterministic bucket (do NOT trust agent code)."""
    key = ("%s:%s" % (flag_name, user_id)).encode("utf-8")
    return int(hashlib.sha256(key).hexdigest()[:8], 16) % 100


def _expected_enrolled(flag_name, pct, enabled, allowlist, users):
    """Ground-truth enrollment recomputed by the oracle itself."""
    out = []
    for u in users:
        if not enabled:
            continue
        if u in allowlist:
            out.append(u)
            continue
        if pct <= 0:
            continue
        if pct >= 100:
            out.append(u)
            continue
        if _oracle_bucket(flag_name, u) < pct:
            out.append(u)
    return out


def _run_child(workdir):
    cfg_path = os.path.join(workdir, "_oracle_roundtrip.json")
    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-c", _CHILD.format(workdir=workdir), cfg_path],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        return None, "could not launch behavioral child: %r" % (e,)
    finally:
        pass
    if proc.returncode != 0:
        return None, "child crashed (rc=%d): %s" % (
            proc.returncode, (proc.stderr or proc.stdout)[-700:])
    if "JSON_START" not in proc.stdout:
        return None, "child produced no JSON:\n%s" % (proc.stdout[-700:],)
    blob = proc.stdout.split("JSON_START", 1)[1].strip()
    try:
        return json.loads(blob), ""
    except Exception as e:  # noqa: BLE001
        return None, "could not parse child JSON (%r): %s" % (e, blob[-400:])


def _check_behavior(workdir):
    data, err = _run_child(workdir)
    if data is None:
        return False, err

    # round-trip via the new key
    if not data.get("to_dict_key_new"):
        return False, "to_dict() must emit the new key and not the old one; got mismatch"
    if data.get("to_dict_value") != 30:
        return False, "to_dict() value wrong: %r (expected 30)" % (data.get("to_dict_value"),)
    if not data.get("from_dict_roundtrip"):
        return False, "from_dict() did not round-trip the new key into rollout_pct"

    users = ["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi", "always_in"]

    exp_mid = _expected_enrolled("oracle_feature", 30, True, ["always_in"], users)
    if data.get("mid_enrolled") != exp_mid:
        return False, ("partial-rollout enrollment wrong: got %r expected %r"
                       % (data.get("mid_enrolled"), exp_mid))

    # disabled flag -> nobody (regression guard)
    if data.get("off_enrolled") != []:
        return False, "disabled flag should enroll nobody, got %r" % (data.get("off_enrolled"),)

    # 0% flag -> only the allowlisted user (regression guard)
    exp_zero = _expected_enrolled("oracle_zero", 0, True, ["vip_zero"], users + ["vip_zero"])
    if data.get("zero_enrolled") != exp_zero:
        return False, "0%% flag should enroll only allowlist, got %r expected %r" % (
            data.get("zero_enrolled"), exp_zero)

    # 100% flag -> everyone (regression guard)
    if data.get("full_enrolled") != users:
        return False, "100%% flag should enroll everyone, got %r" % (data.get("full_enrolled"),)

    # disk round-trip under new key
    if not data.get("disk_has_new_key"):
        return False, "dump_flags did not write the new key to disk"
    if data.get("disk_has_old_key"):
        return False, "dump_flags still wrote the OLD key to disk"
    if data.get("reload_value") != 30:
        return False, "load_flags did not read back the new key (got %r)" % (data.get("reload_value"),)

    # service facade matches engine
    if data.get("svc_mid_enrolled") != exp_mid:
        return False, "FlagService enrollment diverged from engine: %r vs %r" % (
            data.get("svc_mid_enrolled"), exp_mid)

    # override via new kwarg name flips 0% -> 100%
    if data.get("after_override_zero") != users:
        return False, ("override_flag(rollout_pct=100) should enroll everyone, got %r"
                       % (data.get("after_override_zero"),))

    # distractor audit knob intact
    if data.get("sample_pct_attr") == "MISSING":
        return False, "FlagService.sample_percentage distractor was wrongly removed"

    return True, "behavior ok"


def verify(workdir):
    _drop_pyc(workdir)
    try:
        ok, detail = _check_structural(workdir)
        if not ok:
            return False, "[structural] " + detail
        ok, detail = _check_behavior(workdir)
        if not ok:
            return False, "[behavior] " + detail
    except Exception as e:  # noqa: BLE001  -- never raise out of verify
        return False, "[verify-crash] %r" % (e,)
    return True, ("all checks passed: rollout_percentage -> rollout_pct renamed "
                  "consistently across all .py files + flags.json; behavior identical "
                  "on unseen flags/users/config; distractors intact")
