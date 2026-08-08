import os
import re


# Reference fix: write the correct registry.py AND fix the one violating module,
# exactly as a correct agent would after reading every module and applying the
# naming rule. It derives values from the on-disk sources (the "magic" tokens),
# so it stays in lockstep with whatever setup() wrote — no token is hard-coded.

PKG = "plugins"
_VALID = re.compile(r"^cap_[a-z0-9_]+$")
_CAP_RE = re.compile(r'''^(\s*CAPABILITY\s*=\s*)["']([^"']*)["']''', re.M)


def _canonicalize(tok):
    return tok.lower().replace("-", "_")


def apply(workdir):
    pkg_dir = os.path.join(workdir, PKG)

    # 1) Read each module's declared CAPABILITY; fix the one violator IN PLACE.
    final = {}
    for fn in sorted(os.listdir(pkg_dir)):
        if not fn.endswith(".py") or fn == "__init__.py":
            continue
        stem = fn[:-3]
        path = os.path.join(pkg_dir, fn)
        with open(path) as f:
            src = f.read()
        m = _CAP_RE.search(src)
        if not m:
            raise RuntimeError("module %s has no CAPABILITY assignment" % stem)
        tok = m.group(2)
        if not _VALID.match(tok):
            fixed = _canonicalize(tok)
            # rewrite the module's CAPABILITY line to the canonical value
            new_src = _CAP_RE.sub(lambda mm: '%s"%s"' % (mm.group(1), fixed), src, count=1)
            with open(path, "w") as f:
                f.write(new_src)
            tok = fixed
        final[stem] = tok

    # 2) Write the registry at the repo root.
    lines = ['"""registry — module stem -> CAPABILITY token (generated)."""', "", "REGISTRY = {"]
    for stem in sorted(final):
        lines.append('    %r: %r,' % (stem, final[stem]))
    lines.append("}")
    lines.append("")
    with open(os.path.join(workdir, "registry.py"), "w") as f:
        f.write("\n".join(lines))
