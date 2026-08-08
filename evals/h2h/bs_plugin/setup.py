import os
import secrets


# A sliceagent PLUGIN whose register(ctx) contributes a tool to the SAME registry the loop drives
# (see src/sliceagent/plugins.py: _load_one requires BOTH plugin.toml AND __init__.py, then calls
# register(ctx); ctx.register_tool(name, description, handler=..., parameters=...) lands a ToolEntry
# in the registry). The tool returns a MAGIC token that exists nowhere else: the random suffix is
# minted here at setup time, persisted next to the plugin as `.token`, and read back by the handler
# (and by reference_fix). The prompt never contains it and no plain-data file holds it, so the ONLY
# way an agent can put the exact token into release.txt is to actually CALL the plugin tool.
PLUG = "releasekit"

_MANIFEST = '''\
name = "releasekit"
version = "1.0.0"
description = "Release helper: exposes get_release_token."
'''

# The plugin entrypoint. register(ctx) registers get_release_token; the handler returns the EXACT
# token persisted at setup time (read relative to this file, so it is identical across processes and
# across the load-time vs verify-time boundary). The token cannot be guessed — it carries a random
# hex suffix minted by setup().
_INIT = '''\
import os

_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")


def _read_token():
    with open(_TOKEN_FILE, "r") as f:
        return f.read().strip()


def register(ctx):
    """Contribute the release-token tool to the host registry via the plugin facade."""
    def get_release_token(args):
        # The value lives ONLY behind this handler. No inputs needed; ignore args.
        return _read_token()

    ctx.register_tool(
        "get_release_token",
        "Return the current release token for the deploy step. "
        "Takes no arguments; returns the token string.",
        handler=get_release_token,
        parameters={"type": "object", "properties": {}},
    )
'''


def setup(workdir):
    """Install the releasekit plugin into workdir/.sliceagent/plugins/releasekit/ and mint the magic
    release token. Writes plugin.toml + __init__.py (so load_plugins discovers and loads it) and a
    .token file holding the unguessable value the handler returns."""
    pdir = os.path.join(workdir, ".sliceagent", "plugins", PLUG)
    os.makedirs(pdir, exist_ok=True)

    # The magic token: a fixed, recognizable prefix + a random hex suffix minted now. Unguessable.
    token = "RT-7731-" + secrets.token_hex(8)
    with open(os.path.join(pdir, ".token"), "w") as f:
        f.write(token)

    with open(os.path.join(pdir, "plugin.toml"), "w") as f:
        f.write(_MANIFEST)
    with open(os.path.join(pdir, "__init__.py"), "w") as f:
        f.write(_INIT)

    # NOTE: we deliberately do NOT write the token anywhere the agent reads as plain data, and it is
    # not in the prompt. The only path to it is calling the plugin-registered get_release_token tool.
