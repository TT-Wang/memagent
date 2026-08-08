import os


PLUG = "releasekit"


def apply(workdir):
    """Simulate an agent that obtained the release token via the plugin tool and wrote it out.

    We reproduce EXACTLY what an agent that called get_release_token would get — the handler returns
    the token persisted next to the plugin — and write that exact value into release.txt at the
    workspace root. (The handler itself reads this same .token file, so this is identical to the tool
    return value.)
    """
    tok = os.path.join(workdir, ".sliceagent", "plugins", PLUG, ".token")
    with open(tok, "r") as f:
        token = f.read().strip()

    with open(os.path.join(workdir, "release.txt"), "w") as f:
        f.write(token)
