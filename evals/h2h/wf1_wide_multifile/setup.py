import os


def _w(workdir, relpath, content):
    """Write content to workdir/relpath, creating parent dirs."""
    path = os.path.join(workdir, relpath)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def setup(workdir):
    """Build a small but REAL feature-rollout evaluation library spread across
    7 Python modules in a package `rollout/`.

    The library decides, deterministically, whether a given user is "in" a
    feature's gradual rollout. The core concept threaded through the WHOLE
    codebase is a single config field / parameter / dict-key name:

        rollout_percentage   (an int 0..100)

    It appears, in the SEED state, as:
      * a dataclass field on FeatureFlag                 (model.py)
      * a keyword/positional parameter of several funcs  (rules.py, engine.py)
      * a dict key in the on-disk JSON config            (config.py, *.json)
      * a kwarg passed at call sites                      (engine.py, service.py)
      * a value read in conditionals / comparisons       (rules.py, engine.py)
      * a reported field in the audit/report output       (report.py)

    The task (see prompts.json) is to RENAME this one concept end-to-end to
    `rollout_pct` everywhere it refers to THIS field, keeping behavior byte-for-
    byte identical, while NOT touching deliberately-similar distractor names
    (`bucket_percentage`, `sample_percentage`, the literal substring
    "percentage" used only in English prose / help text).

    NO bug is planted: the SEED app runs correctly. The scenario stresses WIDE,
    consistent cross-file propagation of a single rename, not a localized bug.
    """
    pkg = "rollout"

    # ---- package init: re-exports the public surface --------------------
    _w(workdir, os.path.join(pkg, "__init__.py"), '''\
"""rollout: a tiny deterministic feature-rollout evaluation library.

Public surface re-exported for convenience.
"""
from .model import FeatureFlag
from .config import load_flags, dump_flags
from .engine import RolloutEngine
from .service import FlagService
from .errors import RolloutError, UnknownFlagError

__all__ = [
    "FeatureFlag",
    "load_flags",
    "dump_flags",
    "RolloutEngine",
    "FlagService",
    "RolloutError",
    "UnknownFlagError",
]
''')

    # ---- errors: exception hierarchy (no rollout_percentage here) --------
    _w(workdir, os.path.join(pkg, "errors.py"), '''\
"""Exception hierarchy for the rollout library."""


class RolloutError(Exception):
    pass


class UnknownFlagError(RolloutError):
    def __init__(self, name):
        self.name = name
        super().__init__("unknown feature flag %r" % (name,))


class InvalidPercentageError(RolloutError):
    """Raised when a rollout value is outside the inclusive range 0..100."""

    def __init__(self, value):
        self.value = value
        super().__init__("rollout value %r is out of range 0..100" % (value,))
''')

    # ---- hashing helper: the DISTRACTOR bucket_percentage lives here -----
    #   bucket_percentage() returns a stable 0..99 bucket for (flag, user) and
    #   MUST NOT be renamed: it is not the FeatureFlag.rollout_percentage field.
    _w(workdir, os.path.join(pkg, "hashing.py"), '''\
"""Deterministic bucketing. (DISTRACTOR-BEARING module.)

`bucket_percentage` is an INTERNAL helper that maps (flag_name, user_id) to a
stable bucket in 0..99. It is NOT the FeatureFlag rollout field and must keep
its name. The word "percentage" also appears here only as English prose.
"""
import hashlib


def bucket_percentage(flag_name, user_id):
    """Return a stable bucket in 0..99 for this (flag, user).

    The bucket is the rollout *percentage* threshold this user falls under: a
    user is enrolled when their bucket is strictly less than the flag's
    configured rollout value.
    """
    key = ("%s:%s" % (flag_name, user_id)).encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()
    return int(digest[:8], 16) % 100
''')

    # ---- model: FeatureFlag dataclass carrying rollout_percentage --------
    _w(workdir, os.path.join(pkg, "model.py"), '''\
"""The FeatureFlag value object."""
from dataclasses import dataclass, field
from typing import List

from .errors import InvalidPercentageError


@dataclass
class FeatureFlag:
    """A single feature flag.

    rollout_percentage: int in 0..100. 0 disables the flag for everyone; 100
    enables it for everyone; values in between enroll a stable fraction of
    users by bucket.
    """
    name: str
    rollout_percentage: int = 0
    enabled: bool = True
    allowlist: List[str] = field(default_factory=list)

    def validate(self):
        if not isinstance(self.rollout_percentage, int):
            raise InvalidPercentageError(self.rollout_percentage)
        if self.rollout_percentage < 0 or self.rollout_percentage > 100:
            raise InvalidPercentageError(self.rollout_percentage)
        return self

    def to_dict(self):
        return {
            "name": self.name,
            "rollout_percentage": self.rollout_percentage,
            "enabled": self.enabled,
            "allowlist": list(self.allowlist),
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            name=data["name"],
            rollout_percentage=int(data.get("rollout_percentage", 0)),
            enabled=bool(data.get("enabled", True)),
            allowlist=list(data.get("allowlist", [])),
        ).validate()
''')

    # ---- rules: pure decision predicates using rollout_percentage --------
    _w(workdir, os.path.join(pkg, "rules.py"), '''\
"""Pure decision rules. Each takes the rollout value as a parameter named
`rollout_percentage` so the engine can compose them.
"""
from .hashing import bucket_percentage


def is_fully_disabled(rollout_percentage):
    """A rollout value of 0 disables the flag for everyone."""
    return rollout_percentage <= 0


def is_fully_enabled(rollout_percentage):
    """A rollout value of 100 (or more) enables the flag for everyone."""
    return rollout_percentage >= 100


def bucket_enrolled(flag_name, user_id, rollout_percentage):
    """A user is enrolled when their stable bucket is below the rollout value."""
    return bucket_percentage(flag_name, user_id) < rollout_percentage
''')

    # ---- engine: composes the rules; threads rollout_percentage through --
    _w(workdir, os.path.join(pkg, "engine.py"), '''\
"""The RolloutEngine evaluates whether a user is enrolled in a flag."""
from . import rules


class RolloutEngine:
    """Stateless evaluator over a dict of {name: FeatureFlag}."""

    def __init__(self, flags):
        # flags: dict mapping flag name -> FeatureFlag
        self.flags = dict(flags)

    def _decide(self, flag_name, user_id, rollout_percentage, enabled, allowlist):
        """Core decision, expressed purely in terms of the rollout value."""
        if not enabled:
            return False
        if user_id in allowlist:
            return True
        if rules.is_fully_disabled(rollout_percentage):
            return False
        if rules.is_fully_enabled(rollout_percentage):
            return True
        return rules.bucket_enrolled(flag_name, user_id, rollout_percentage)

    def is_enrolled(self, flag_name, user_id):
        from .errors import UnknownFlagError
        flag = self.flags.get(flag_name)
        if flag is None:
            raise UnknownFlagError(flag_name)
        return self._decide(
            flag_name,
            user_id,
            rollout_percentage=flag.rollout_percentage,
            enabled=flag.enabled,
            allowlist=flag.allowlist,
        )

    def enrolled_users(self, flag_name, user_ids):
        """Return the subset of user_ids enrolled in flag_name (input order)."""
        return [u for u in user_ids if self.is_enrolled(flag_name, u)]
''')

    # ---- config: load/dump flags as JSON; key is "rollout_percentage" ---
    _w(workdir, os.path.join(pkg, "config.py"), '''\
"""Load and dump feature flags to/from JSON on disk.

The JSON object for each flag uses the key "rollout_percentage". A separate,
UNRELATED top-level field "sample_percentage" controls audit sampling and must
NOT be confused with the per-flag rollout value (it is a distractor).
"""
import json

from .model import FeatureFlag


def load_flags(path):
    """Read a JSON config file and return {name: FeatureFlag}."""
    with open(path, "r") as f:
        data = json.load(f)
    flags = {}
    for item in data.get("flags", []):
        flag = FeatureFlag.from_dict(item)
        flags[flag.name] = flag
    return flags


def dump_flags(flags, path):
    """Write {name: FeatureFlag} back to a JSON config file."""
    payload = {
        "version": 1,
        # sample_percentage is an audit-sampling knob, NOT a rollout value.
        "sample_percentage": 10,
        "flags": [flag.to_dict() for flag in flags.values()],
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    return path
''')

    # ---- report: human-readable audit; reads rollout_percentage ---------
    _w(workdir, os.path.join(pkg, "report.py"), '''\
"""Render a human-readable audit line for each flag."""


def summarize_flag(flag):
    """One-line summary of a FeatureFlag's rollout configuration."""
    state = "on" if flag.enabled else "off"
    return "%s: %d%% [%s] allow=%d" % (
        flag.name,
        flag.rollout_percentage,
        state,
        len(flag.allowlist),
    )


def summarize_all(flags):
    """Return a stable, sorted multi-line report for all flags."""
    lines = [summarize_flag(flags[name]) for name in sorted(flags)]
    return "\\n".join(lines)
''')

    # ---- service: top-level facade tying config + engine together -------
    #   Also exercises a kwarg call site (override_flag) and a DISTRACTOR
    #   field `sample_percentage` that must NOT be renamed.
    _w(workdir, os.path.join(pkg, "service.py"), '''\
"""High-level facade: load flags, evaluate users, override values."""
from .config import load_flags
from .engine import RolloutEngine
from .model import FeatureFlag
from .report import summarize_all
from .errors import UnknownFlagError


class FlagService:
    """Loads flags once and answers enrollment + reporting queries."""

    def __init__(self, flags, sample_percentage=10):
        # sample_percentage is an UNRELATED audit-sampling rate (distractor).
        self.flags = dict(flags)
        self.sample_percentage = sample_percentage
        self.engine = RolloutEngine(self.flags)

    @classmethod
    def from_file(cls, path):
        return cls(load_flags(path))

    def is_enrolled(self, flag_name, user_id):
        return self.engine.is_enrolled(flag_name, user_id)

    def enrolled_users(self, flag_name, user_ids):
        return self.engine.enrolled_users(flag_name, user_ids)

    def override_flag(self, flag_name, rollout_percentage):
        """Set a new rollout value for an existing flag and rebuild the engine."""
        flag = self.flags.get(flag_name)
        if flag is None:
            raise UnknownFlagError(flag_name)
        updated = FeatureFlag(
            name=flag.name,
            rollout_percentage=rollout_percentage,
            enabled=flag.enabled,
            allowlist=list(flag.allowlist),
        ).validate()
        self.flags[flag_name] = updated
        self.engine = RolloutEngine(self.flags)
        return updated

    def report(self):
        return summarize_all(self.flags)
''')

    # ---- on-disk seed config (uses the "rollout_percentage" JSON key) ----
    _w(workdir, "flags.json", '''\
{
  "version": 1,
  "sample_percentage": 10,
  "flags": [
    {"name": "new_checkout", "rollout_percentage": 50, "enabled": true, "allowlist": ["vip1"]},
    {"name": "dark_mode", "rollout_percentage": 0, "enabled": true, "allowlist": ["designer"]},
    {"name": "beta_search", "rollout_percentage": 100, "enabled": true, "allowlist": []},
    {"name": "legacy_banner", "rollout_percentage": 25, "enabled": false, "allowlist": []}
  ]
}
''')

    # ---- a runnable demo + README documenting the public field name ------
    _w(workdir, "demo.py", '''\
"""Tiny demo entry point exercising the rollout library end-to-end."""
from rollout import FlagService


def main():
    svc = FlagService.from_file("flags.json")
    users = ["u1", "u2", "u3", "vip1", "designer"]
    print(svc.report())
    print("checkout enrolled:", svc.enrolled_users("new_checkout", users))
    svc.override_flag("dark_mode", rollout_percentage=100)
    print("dark_mode now:", svc.enrolled_users("dark_mode", users))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''')

    _w(workdir, "README.md", '''\
# rollout

A tiny deterministic feature-rollout evaluation library.

Each feature flag carries an integer **rollout percentage** (0..100) under the
field/key name `rollout_percentage`. A user is enrolled when their stable hash
bucket falls under the flag's `rollout_percentage` (allowlisted users are always
enrolled; disabled flags never enroll).

```python
from rollout import FlagService

svc = FlagService.from_file("flags.json")
svc.is_enrolled("new_checkout", "u1")
svc.override_flag("new_checkout", rollout_percentage=75)
```

Notes:
- `rollout_percentage` is the per-flag rollout value, used as a dataclass field
  (`model.py`), a function parameter (`rules.py`, `engine.py`), a JSON config
  key (`config.py`, `flags.json`), a kwarg at call sites, and a reported field
  (`report.py`).
- `bucket_percentage` (in `hashing.py`) is an UNRELATED internal helper.
- `sample_percentage` (in `config.py` / `service.py`) is an UNRELATED audit knob.
''')
