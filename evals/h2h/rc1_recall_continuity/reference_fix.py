import os

# Correct, full implementation of appconf after all SEVEN turns.
# VALIDATION ONLY -- never shown to the benchmarked agents.
#
# The recall-critical bits (only established in turn 1, never restated in turn 7):
#   * new keys are UPPER_SNAKE with the APP_ prefix  -> APP_TIMEOUT, APP_MAX_RETRIES
#   * rendered config uses a 2-space indent unit      -> render.INDENT == 2
#   * the default service port is 8137                -> APP_PORT default 8137
# Turns 2-6 are small unrelated edits; they are applied here too so the final
# state matches what a faithful agent would produce, and so the regression
# guards in verify.py (docstring text, kinds() single-source, cli --help,
# option_count export) are satisfiable.

# ---- appconf/__init__.py (turn 5 adds option_count to __all__) --------------
INIT = '''\
"""appconf: a tiny typed configuration-schema library."""
from .schema import Option, REGISTRY, register, get_option, option_count
from .render import render_config
from . import defaults as _defaults

_defaults.install()

__all__ = [
    "Option", "REGISTRY", "register", "get_option", "option_count",
    "render_config",
]
'''

# ---- appconf/defaults.py (turn 1: APP_PORT; turn 7: APP_TIMEOUT, APP_MAX_RETRIES)
DEFAULTS = '''\
"""Install the project's default configuration options into the registry."""
from .schema import register, get_option


def install():
    """Register the project's default options. Idempotent."""
    if get_option("APP_PORT") is None:
        register("APP_PORT", 8137, "int", "service bind port")
    if get_option("APP_TIMEOUT") is None:
        register("APP_TIMEOUT", 30, "int", "request timeout in seconds")
    if get_option("APP_MAX_RETRIES") is None:
        register("APP_MAX_RETRIES", 5, "int", "max request retry attempts")
'''

# ---- appconf/schema.py (turn 3: kinds() single source; turn 5: option_count) -
SCHEMA = '''\
"""Typed configuration options and the global registry."""


def kinds():
    """Return the tuple of valid option kinds (single source of truth)."""
    return ("int", "str", "bool")


class Option:
    __slots__ = ("name", "default", "kind", "help")

    def __init__(self, name, default, kind, help=""):
        if kind not in kinds():
            raise ValueError("unknown kind: %r" % (kind,))
        self.name = name
        self.default = default
        self.kind = kind
        self.help = help

    def coerce(self, raw):
        if self.kind == "int":
            return int(raw)
        if self.kind == "bool":
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        return str(raw)

    def __repr__(self):
        return "Option(%r, default=%r, kind=%r)" % (self.name, self.default, self.kind)


REGISTRY = []


def register(name, default, kind, help=""):
    for existing in REGISTRY:
        if existing.name == name:
            raise ValueError("option already registered: %r" % (name,))
    opt = Option(name, default, kind, help)
    REGISTRY.append(opt)
    return opt


def get_option(name):
    for opt in REGISTRY:
        if opt.name == name:
            return opt
    return None


def option_count():
    """Return the number of currently-registered options."""
    return len(REGISTRY)
'''

# ---- appconf/render.py (turn 2: docstring blank-line note; turn 6: inline note)
RENDER = '''\
"""Render the option registry into a hierarchical ``.conf`` document.

Layout::

    [<section>]
    <indent><KEY> = <value>   ; <help>

Sections come from the option name: the token between the house prefix and the
rest (lower-cased). All keys in a section are indented one level under the
``[section]`` header, and a single blank line separates one section from the
next.
"""
from .schema import REGISTRY


# The per-level indent unit, in spaces. (House style.)
INDENT = 2


def _section_of(name):
    if "_" not in name:
        return "default"
    return name.split("_", 1)[0].lower()


def _fmt_value(opt):
    if opt.kind == "bool":
        return "true" if opt.default else "false"
    return str(opt.default)


def render_config(registry=None):
    reg = REGISTRY if registry is None else registry
    pad = " " * INDENT
    sections = []
    by_section = {}
    for opt in reg:
        sec = _section_of(opt.name)
        if sec not in by_section:
            by_section[sec] = []
            sections.append(sec)
        by_section[sec].append(opt)

    lines = []
    for sec in sections:
        lines.append("[%s]" % sec)
        for opt in by_section[sec]:
            line = "%s%s = %s" % (pad, opt.name, _fmt_value(opt))
            if opt.help:
                line += "   ; %s" % opt.help
            lines.append(line)
        lines.append("")
    # The blank line emitted after the last section is intentionally stripped
    # so the document does not end with a trailing blank section separator.
    return "\\n".join(lines).rstrip("\\n") + "\\n"
'''

# ---- appconf/server.py (turn 1: wire bind_address to APP_PORT) ---------------
SERVER = '''\
"""A minimal server stub that reads its bind port from the config registry."""
from .schema import get_option


def bind_address(host="0.0.0.0"):
    opt = get_option("APP_PORT")
    if opt is None:
        raise RuntimeError("service port option is not registered yet")
    return (host, int(opt.default))
'''

# ---- appconf/client.py (turn 7: new consumer stub) --------------------------
CLIENT = '''\
"""A minimal HTTP-client stub that reads its timeout from the config registry."""
from .schema import get_option


def timeout_seconds():
    opt = get_option("APP_TIMEOUT")
    if opt is None:
        raise RuntimeError("request timeout option is not registered yet")
    return int(opt.default)
'''

# ---- appconf/cli.py (turn 4: --help / -h flag) ------------------------------
CLI = '''\
"""A very small command-line front end for inspecting the config."""
import sys
from .render import render_config


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--help", "-h"):
        sys.stdout.write("usage: appconf [--help]\\n")
        return 0
    sys.stdout.write(render_config())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def apply(workdir):
    pkg = os.path.join(workdir, "appconf")
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write(INIT)
    with open(os.path.join(pkg, "defaults.py"), "w") as f:
        f.write(DEFAULTS)
    with open(os.path.join(pkg, "schema.py"), "w") as f:
        f.write(SCHEMA)
    with open(os.path.join(pkg, "render.py"), "w") as f:
        f.write(RENDER)
    with open(os.path.join(pkg, "server.py"), "w") as f:
        f.write(SERVER)
    with open(os.path.join(pkg, "client.py"), "w") as f:
        f.write(CLIENT)
    with open(os.path.join(pkg, "cli.py"), "w") as f:
        f.write(CLI)
