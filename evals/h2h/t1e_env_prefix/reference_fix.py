import os

# Correct, full implementation of the confkit loader after all seven turns.
# VALIDATION ONLY -- never shown to the benchmarked agents.
#
# The buried turn-3 aside that makes the detail probe pass:
#   every env var the loader consults carries the APPCFG_ prefix; a bare
#   name (TIMEOUT, DB_PORT, ...) is NEVER read. Hence _env_name() below
#   prepends _ENV_PREFIX and nothing else in the module touches os.environ.

REFERENCE = '''\
"""confkit: a small config loader for KEY = VALUE files with [section] support.

Design notes:
  * Flat ``key = value`` lines; a ``[section]`` header scopes the following
    keys to dotted names like ``server.port``. Sections/keys normalize to
    lowercase. Blank lines and full-line #/; comments are skipped.
  * Values are coerced: int, float, true/false booleans; quoted values stay
    strings with the quotes stripped.
  * Precedence (low -> high): defaults, file values (later files beat
    earlier ones in load_files), environment overrides.
  * Environment overrides: a config key maps to APPCFG_ + KEY uppercased
    with dots turned into underscores. ONLY the APPCFG_-prefixed name is
    ever consulted -- bare names collide with the deploy tooling.
"""
import os


class ConfigError(Exception):
    pass


_ENV_PREFIX = "APPCFG_"


def _coerce(raw):
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _parse(text, source="<config>"):
    data = {}
    section = None
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].strip().lower()
            if not name:
                raise ConfigError(
                    "%s: empty section header on line %d" % (source, lineno))
            section = name
            continue
        if "=" not in stripped:
            raise ConfigError(
                "%s: malformed line %d: %r" % (source, lineno, stripped))
        key, _, raw = stripped.partition("=")
        key = key.strip().lower()
        if not key:
            raise ConfigError(
                "%s: missing key on line %d" % (source, lineno))
        full = section + "." + key if section else key
        data[full] = _coerce(raw)
    return data


def _read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError as exc:
        raise ConfigError("cannot read config file %s: %s" % (path, exc))


def _env_name(key):
    return _ENV_PREFIX + key.upper().replace(".", "_")


class Config:
    def __init__(self, data=None, defaults=None):
        self._data = dict(data or {})
        self._defaults = dict(defaults or {})

    @classmethod
    def load(cls, path, defaults=None):
        return cls(_parse(_read_file(path), source=path), defaults=defaults)

    @classmethod
    def load_files(cls, paths, defaults=None):
        merged = {}
        for p in paths:
            merged.update(_parse(_read_file(p), source=p))
        return cls(merged, defaults=defaults)

    def as_dict(self):
        merged = dict(self._defaults)
        merged.update(self._data)
        for key in list(merged):
            env_name = _env_name(key)
            if env_name in os.environ:
                merged[key] = _coerce(os.environ[env_name])
        return merged

    def get(self, key, default=None):
        return self.as_dict().get(key, default)

    def require(self, *keys):
        merged = self.as_dict()
        missing = sorted(k for k in keys if k not in merged)
        if missing:
            raise ConfigError(
                "missing required config keys: " + ", ".join(missing))
        return None
'''

REFERENCE_INIT = "from .loader import Config, ConfigError\n"


def apply(workdir):
    loader = os.path.join(workdir, "confkit", "loader.py")
    with open(loader, "w") as f:
        f.write(REFERENCE)
    init = os.path.join(workdir, "confkit", "__init__.py")
    with open(init, "w") as f:
        f.write(REFERENCE_INIT)
