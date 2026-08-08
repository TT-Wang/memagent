import os

# Seed project: ``appconf`` -- a small, real configuration-schema library.
#
# The library keeps a REGISTRY of typed configuration options and can render the
# whole registry into a hierarchical ``.conf`` text. The rendering uses nested
# sections that are indented; the seed renderer is written with a 2-SPACE indent
# unit. Option names follow a house convention: every key is prefixed ``APP_``.
#
# This is the starting repo BEFORE turn 1. Seven user turns extend it. The first
# turn establishes three DURABLE house conventions by having the agent apply them
# to a concrete first change:
#     (1) every config key is prefixed ``APP_``,
#     (2) rendered config uses a 2-SPACE indent unit,
#     (3) the default service port is 8137.
# Turns 2-6 are small unrelated edits in other parts of the package. The FINAL
# turn asks for new config options WITHOUT restating any of the three
# conventions; honoring them requires recalling what turn 1 established.
#
# The conventions are NOT redundantly documented in a file the final-turn agent
# would naturally open: schema.py has no validator that enforces the prefix, the
# renderer's indent unit is a bare numeric literal (no "2-space" comment), and
# the port number lives only in the option the agent registers in turn 1. So an
# agent that has lost the turn-1 context cannot re-derive the conventions by a
# quick local read; it must have carried/recalled them.

SEED_INIT = '''\
"""appconf: a tiny typed configuration-schema library.

Importing this package installs the project's default options into the global
registry by calling :func:`appconf.defaults.install`. New options that should
ship by default are registered there so they are present after a bare
``import appconf``.
"""
from .schema import Option, REGISTRY, register, get_option
from .render import render_config
from . import defaults as _defaults

_defaults.install()

__all__ = ["Option", "REGISTRY", "register", "get_option", "render_config"]
'''

# defaults.py: the single import-time home for default option registrations.
# Seed ships install() empty; turn 1 registers the service port here, and the
# final turn adds the new HTTP-client options here too.
SEED_DEFAULTS = '''\
"""Install the project's default configuration options into the registry.

:func:`install` is called once when ``appconf`` is imported. Register default
options here (via :func:`appconf.schema.register`) so they are present after a
bare ``import appconf``. ``install`` is idempotent: it no-ops if the options it
would add are already registered.
"""
from .schema import register, get_option


def install():
    """Register the project's default options. Idempotent."""
    # (Default options are registered here.)
    return
'''

# schema.py: the registry of typed options. NOTE: register() deliberately does
# NOT enforce any name convention -- the APP_ prefix is a house rule the team
# follows by hand, not a validated invariant. This is what makes the final turn
# a genuine recall test rather than something a local read can backfill.
SEED_SCHEMA = '''\
"""Typed configuration options and the global registry.

An :class:`Option` is one configuration knob: a fully-qualified ``name``, a
``default`` value, a ``kind`` (one of "int", "str", "bool"), and a one-line
``help`` string. The module keeps a single ordered ``REGISTRY`` of options;
:func:`render_config` (in render.py) turns the registry into a ``.conf`` file.

Options are grouped into sections by the part of the name before the first
underscore that follows the house prefix (e.g. ``APP_PORT`` -> section "app";
``APP_DB_HOST`` -> section "app"). The renderer indents the keys under their
section header.
"""


class Option:
    __slots__ = ("name", "default", "kind", "help")

    def __init__(self, name, default, kind, help=""):
        if kind not in ("int", "str", "bool"):
            raise ValueError("unknown kind: %r" % (kind,))
        self.name = name
        self.default = default
        self.kind = kind
        self.help = help

    def coerce(self, raw):
        """Coerce a raw string to this option's kind."""
        if self.kind == "int":
            return int(raw)
        if self.kind == "bool":
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        return str(raw)

    def __repr__(self):
        return "Option(%r, default=%r, kind=%r)" % (self.name, self.default, self.kind)


# The global registry. Seed ships EMPTY on purpose: turn 1 registers the first
# real option (the service port) and, in doing so, fixes the house conventions.
REGISTRY = []


def register(name, default, kind, help=""):
    """Append a new option to the registry and return it.

    Raises if an option with the same name is already registered, so the
    registry never contains duplicates.
    """
    for existing in REGISTRY:
        if existing.name == name:
            raise ValueError("option already registered: %r" % (name,))
    opt = Option(name, default, kind, help)
    REGISTRY.append(opt)
    return opt


def get_option(name):
    """Return the registered Option with this name, or None."""
    for opt in REGISTRY:
        if opt.name == name:
            return opt
    return None
'''

# render.py: turns the REGISTRY into a hierarchical .conf text. The indent unit
# is a numeric literal with NO "two spaces" comment -- the only way to know the
# house indent is 2 is to have seen/applied it (turn 1 wires it in).
SEED_RENDER = '''\
"""Render the option registry into a hierarchical ``.conf`` document.

Layout::

    [<section>]
    <indent><KEY> = <value>   ; <help>

Sections come from the option name: the token between the house prefix and the
rest (lower-cased). All keys in a section are indented one level under the
``[section]`` header.
"""
from .schema import REGISTRY


# The per-level indent unit, in spaces. (House style.)
INDENT = 2


def _section_of(name):
    """Derive a section label from an option name.

    ``APP_PORT`` -> "app"; ``APP_DB_HOST`` -> "app". The section is the first
    token of the name, lower-cased. Options that do not contain an underscore
    fall into the "default" section.
    """
    if "_" not in name:
        return "default"
    return name.split("_", 1)[0].lower()


def _fmt_value(opt):
    if opt.kind == "bool":
        return "true" if opt.default else "false"
    return str(opt.default)


def render_config(registry=None):
    """Render the given registry (default: the global REGISTRY) to text.

    Returns a single string. Keys are grouped by section in first-seen order,
    and each key line is indented by one INDENT unit under its section header.
    """
    reg = REGISTRY if registry is None else registry
    pad = " " * INDENT
    sections = []          # ordered list of section names
    by_section = {}        # section -> list[Option]
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
        lines.append("")  # blank line between sections
    return "\\n".join(lines).rstrip("\\n") + "\\n"
'''

# server.py: a tiny consumer of the port option. Turn 1 wires the port option
# into here. Seed has a hard "TODO" placeholder so the seed is importable but
# does not yet know the port.
SEED_SERVER = '''\
"""A minimal server stub that reads its bind port from the config registry."""
from .schema import get_option


def bind_address(host="0.0.0.0"):
    """Return an (host, port) tuple for the server to bind.

    The port comes from the registered service-port option. Until that option
    is registered (turn 1), there is no port to bind and this raises.
    """
    opt = get_option("APP_PORT")
    if opt is None:
        raise RuntimeError("service port option is not registered yet")
    return (host, int(opt.default))
'''

SEED_CLI = '''\
"""A very small command-line front end for inspecting the config."""
import sys
from .render import render_config


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Print the rendered configuration to stdout.
    sys.stdout.write(render_config())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

SEED_README = '''\
# appconf

A tiny typed configuration-schema library.

* `appconf/schema.py` -- the `Option` type and the global `REGISTRY`, plus
  `register()` / `get_option()`.
* `appconf/render.py` -- `render_config()` turns the registry into a
  hierarchical `.conf` document.
* `appconf/server.py` -- a stub consumer that binds to the service port.
* `appconf/cli.py` -- prints the rendered config.

The registry starts empty; options are added with `register(name, default,
kind, help)`.
'''

SEED_TEST = '''\
"""Smoke tests that ship with the seed. They exercise only the seed surface
(the Option type and an empty-registry render). Keep them green."""
from appconf.schema import Option, REGISTRY, register, get_option
from appconf.render import render_config


def test_option_coerce():
    o = Option("APP_X", 1, "int")
    assert o.coerce("42") == 42
    b = Option("APP_Y", False, "bool")
    assert b.coerce("yes") is True
    assert b.coerce("no") is False


def test_register_roundtrip():
    REGISTRY.clear()
    register("APP_SAMPLE", 3, "int", "a sample")
    assert get_option("APP_SAMPLE").default == 3
    REGISTRY.clear()


def test_render_empty():
    REGISTRY.clear()
    assert render_config() == "\\n"


if __name__ == "__main__":
    test_option_coerce()
    test_register_roundtrip()
    test_render_empty()
    print("seed tests ok")
'''


def setup(workdir):
    pkg = os.path.join(workdir, "appconf")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write(SEED_INIT)
    with open(os.path.join(pkg, "schema.py"), "w") as f:
        f.write(SEED_SCHEMA)
    with open(os.path.join(pkg, "defaults.py"), "w") as f:
        f.write(SEED_DEFAULTS)
    with open(os.path.join(pkg, "render.py"), "w") as f:
        f.write(SEED_RENDER)
    with open(os.path.join(pkg, "server.py"), "w") as f:
        f.write(SEED_SERVER)
    with open(os.path.join(pkg, "cli.py"), "w") as f:
        f.write(SEED_CLI)
    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write(SEED_README)
    tests = os.path.join(workdir, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(tests, "test_seed.py"), "w") as f:
        f.write(SEED_TEST)
