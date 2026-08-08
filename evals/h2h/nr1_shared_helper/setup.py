import os


def _w(workdir, relpath, content):
    path = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def setup(workdir):
    pkg = "settingskit"

    _w(workdir, os.path.join(pkg, "__init__.py"), '''\
"""settingskit: a tiny layered settings reader (env -> file -> default)."""
from .core.config import get_setting

__all__ = ["get_setting"]
''')

    # THE SHARED HELPER the task changes (value -> (value, source) tuple).
    _w(workdir, os.path.join(pkg, "core", "__init__.py"), "")
    _w(workdir, os.path.join(pkg, "core", "config.py"), '''\
"""Layered settings: environment overrides the file store, which overrides defaults."""
import os

_FILE = {"db_url": "sqlite:///app.db", "workers": "4", "timeout": "30"}
_DEFAULT = {"db_url": "sqlite:///:memory:", "workers": "1", "timeout": "10"}


def get_setting(key):
    if key in os.environ:
        return os.environ[key]
    if key in _FILE:
        return _FILE[key]
    return _DEFAULT.get(key, "")
''')

    # OBVIOUS caller (same-ish area).
    _w(workdir, os.path.join(pkg, "app", "__init__.py"), "")
    _w(workdir, os.path.join(pkg, "app", "api.py"), '''\
from ..core.config import get_setting


def db_banner():
    return "DB=" + get_setting("db_url")
''')

    _w(workdir, os.path.join(pkg, "app", "report.py"), '''\
from ..core.config import get_setting


def timeout_line():
    return "timeout=" + get_setting("timeout") + "s"
''')

    # DISTANT caller (different subpackage) — the one a partial fix misses.
    _w(workdir, os.path.join(pkg, "jobs", "__init__.py"), "")
    _w(workdir, os.path.join(pkg, "jobs", "scheduler.py"), '''\
from ..core.config import get_setting


def max_workers():
    return int(get_setting("workers") or "1")
''')

    # DISTRACTOR: an UNRELATED helper that merely shares a name pattern; must NOT be edited.
    _w(workdir, os.path.join(pkg, "util", "__init__.py"), "")
    _w(workdir, os.path.join(pkg, "util", "strings.py"), '''\
def get_setting_label(mapping, key):
    """Format a key/value label from an arbitrary dict — NOT the config getter."""
    return "%s=%s" % (key, mapping.get(key, "?"))
''')
