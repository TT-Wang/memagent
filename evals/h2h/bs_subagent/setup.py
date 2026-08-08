import os


# -----------------------------------------------------------------------------
# bs_subagent — a BREADTH scenario for the `subagents` capability.
#
# A `plugins` package of 13 sibling modules, each declaring exactly one
# top-level  CAPABILITY = "<token>"  assignment. The tokens are UNGUESSABLE
# per-module strings embedded ONLY in the source, so a correct registry cannot
# be produced without actually reading every module (the breadth). Delegating
# the reads to read-only explorer children is the natural strategy, but a single
# agent that reads all 13 itself can pass too — the scenario does not force
# delegation.
#
# Naming rule (NAMING_RULE.md): every CAPABILITY must be lowercase snake_case
# beginning with the prefix "cap_". Exactly ONE module (plug_quartz) violates it
# — its value is "Cap-Quartz-D4B2" — and the REQUIRED fixed form is the
# deterministic canonicalization "cap_quartz_d4b2".
#
# The task: WRITE registry.py at the package root mapping module-stem ->
# capability for ALL 13 modules (using the FIXED value for the violator), and
# EDIT plug_quartz.py so its CAPABILITY equals the fixed value.
# -----------------------------------------------------------------------------

PKG = "plugins"

# The single source of truth for module -> CAPABILITY token. These tokens are
# the "magic": arbitrary, unguessable strings that exist ONLY in the files we
# write here. The oracle re-reads them from disk (it does NOT import this dict),
# so authoring and verification share one ground truth through the filesystem.
#
# 12 of the 13 already obey the rule (lowercase snake_case, "cap_" prefix). The
# 13th (plug_quartz) deliberately violates it.
GOOD_CAPS = {
    "plug_alpha":   "cap_alpha_7f3a9",
    "plug_bravo":   "cap_bravo_19c40",
    "plug_cinnamon":"cap_cinnamon_a02e",
    "plug_delta":   "cap_delta_5b7711",
    "plug_echo":    "cap_echo_ee19c2",
    "plug_foxtrot": "cap_foxtrot_3344d",
    "plug_garnet":  "cap_garnet_90af1",
    "plug_harbor":  "cap_harbor_c7d28",
    "plug_indigo":  "cap_indigo_2e6b0",
    "plug_juniper": "cap_juniper_f10aa",
    "plug_kestrel": "cap_kestrel_88b3e",
    "plug_lumen":   "cap_lumen_4d9c6",
}

# The ONE violating module: wrong case + hyphens (not snake_case, not "cap_"
# lowercase prefix). Its required canonical fixed form is given alongside.
VIOLATOR_STEM = "plug_quartz"
VIOLATOR_BAD = "Cap-Quartz-D4B2"
VIOLATOR_FIXED = "cap_quartz_d4b2"   # lowercase, hyphens -> underscores, "cap_" prefix

# A couple of light, plausible bodies so the modules read like real plugins
# rather than one-line stubs (breadth should feel real). The CAPABILITY line is
# always the load-bearing top-level assignment the oracle keys on.
_BODY_TEMPLATES = [
    (
        'def register(registry):\n'
        '    """Register this plugin under its capability key."""\n'
        '    registry[CAPABILITY] = {}\n'
        '    return registry\n'
    ),
    (
        'def describe():\n'
        '    """Human-readable one-liner for this plugin."""\n'
        '    return "%s plugin (%s)" % (__name__, CAPABILITY)\n'
    ),
    (
        'ENABLED = True\n\n\n'
        'def capability():\n'
        '    """Accessor for this plugin\'s capability token."""\n'
        '    return CAPABILITY\n'
    ),
]


def _module_source(stem, cap, idx):
    body = _BODY_TEMPLATES[idx % len(_BODY_TEMPLATES)]
    return (
        '"""%s — a plugin module.\n\n'
        'Every plugin declares its CAPABILITY token (see ../NAMING_RULE.md).\n'
        '"""\n\n'
        'CAPABILITY = "%s"\n\n\n'
        '%s'
    ) % (stem, cap, body)


def setup(workdir):
    pkg_dir = os.path.join(workdir, PKG)
    os.makedirs(pkg_dir, exist_ok=True)

    # 1) The 12 well-formed plugin modules.
    all_modules = list(GOOD_CAPS.items())
    # 2) plus the 1 violator -> 13 total.
    all_modules.append((VIOLATOR_STEM, VIOLATOR_BAD))

    for idx, (stem, cap) in enumerate(all_modules):
        path = os.path.join(pkg_dir, stem + ".py")
        with open(path, "w") as f:
            f.write(_module_source(stem, cap, idx))

    # 3) Package __init__: a real (if tiny) plugin package. It does NOT enumerate
    #    capabilities — discovering them is the agent's job (breadth).
    init_src = (
        '"""plugins — a small plugin package.\n\n'
        'Each sibling module declares a top-level CAPABILITY = "<token>".\n'
        'There is NO central list here on purpose: the registry must be built\n'
        'by reading every module. See ../NAMING_RULE.md for the naming rule and\n'
        'the registry task.\n'
        '"""\n'
    )
    with open(os.path.join(pkg_dir, "__init__.py"), "w") as f:
        f.write(init_src)

    # 4) The naming rule + task description (no internal token leaked here).
    rule = (
        "Plugin capability naming rule & registry task\n"
        "=============================================\n\n"
        "Every module in the `plugins/` package declares exactly one top-level\n"
        "assignment of the form:\n\n"
        '    CAPABILITY = "<token>"\n\n'
        "NAMING RULE\n"
        "-----------\n"
        "Every CAPABILITY token MUST be lowercase snake_case beginning with the\n"
        "prefix `cap_`. Concretely a valid token matches `^cap_[a-z0-9_]+$`:\n"
        "all-lowercase, words separated by single underscores, starting `cap_`.\n\n"
        "Most modules already obey this. EXACTLY ONE module violates the rule\n"
        "(wrong case and/or wrong separators). It must be fixed by canonicalizing\n"
        "its token: lowercase every letter and replace each hyphen `-` with an\n"
        "underscore `_` (e.g. `Cap-Foo-Bar` -> `cap_foo_bar`). Do not invent a\n"
        "new token — derive the fixed value from the existing one.\n\n"
        "TASKS\n"
        "-----\n"
        "1. Build a registry. Create a file `registry.py` at the repo root (the\n"
        "   parent of `plugins/`, i.e. the same directory as this file) that\n"
        "   defines a single top-level dict named `REGISTRY` mapping each\n"
        "   module's stem (its filename without `.py`, e.g. `plug_alpha`) to that\n"
        "   module's CAPABILITY token, for ALL modules in the package. Use the\n"
        "   FIXED value for the one module you correct.\n\n"
        "2. Fix the violator. Edit the one offending module so its CAPABILITY\n"
        "   equals the canonicalized value, satisfying the naming rule.\n\n"
        "After your changes, `registry.REGISTRY` must agree with what each\n"
        "module actually declares on disk, and every value must satisfy the rule.\n"
    )
    with open(os.path.join(workdir, "NAMING_RULE.md"), "w") as f:
        f.write(rule)

    # 5) A tiny demo that imports the package (so the repo "runs"); it does not
    #    reveal the tokens.
    demo = (
        '"""demo — imports the plugins package (smoke check only)."""\n'
        'import importlib\n'
        'import os\n'
        'import plugins\n\n\n'
        'def list_module_stems():\n'
        '    here = os.path.dirname(plugins.__file__)\n'
        '    return sorted(\n'
        '        fn[:-3] for fn in os.listdir(here)\n'
        '        if fn.endswith(".py") and fn != "__init__.py"\n'
        '    )\n\n\n'
        'if __name__ == "__main__":\n'
        '    for stem in list_module_stems():\n'
        '        m = importlib.import_module("plugins." + stem)\n'
        '        print(stem, "->", m.CAPABILITY)\n'
    )
    with open(os.path.join(workdir, "demo.py"), "w") as f:
        f.write(demo)
