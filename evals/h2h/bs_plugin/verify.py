import os


PLUG = "releasekit"


def _magic_token(workdir):
    """The oracle's source of truth: the exact token the plugin handler returns. It is the value
    persisted at setup time next to the plugin — identical to what get_release_token yields. The
    agent can only have this in release.txt if it actually invoked the plugin tool."""
    tok = os.path.join(workdir, ".sliceagent", "plugins", PLUG, ".token")
    with open(tok, "r") as f:
        return f.read().strip()


def verify(workdir):
    rel = os.path.join(workdir, "release.txt")
    if not os.path.isfile(rel):
        return False, "release.txt not found in workspace root"

    try:
        expected = _magic_token(workdir)
    except OSError:
        return False, "plugin .token missing — setup did not run"

    with open(rel, "r") as f:
        got = f.read().strip()

    if not got:
        return False, "release.txt is empty"
    if got != expected:
        # Show only short prefixes; the full magic value is unguessable by design.
        return False, ("release.txt token mismatch: got %r, expected the exact token returned by "
                       "get_release_token (prefix %r…)" % (got[:16], expected[:11]))

    return True, ("release.txt contains the exact plugin token %r… — obtainable only by calling "
                  "the plugin-registered get_release_token tool" % (expected[:11],))
