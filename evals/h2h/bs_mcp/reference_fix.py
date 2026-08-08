import os


# The reference "fix" simulates what a correct agent does: obtain the build id THROUGH the specsvc MCP
# mechanism and write it to build_id.txt. It reads the per-run secret the server itself reads at startup
# (the same value the live MCP tool returns), so the fix and the oracle never drift. Deterministic, offline.


def _id_from_secret(workdir):
    secret = os.path.join(workdir, ".specsvc", "build_id.secret")
    with open(secret, encoding="utf-8") as f:
        return f.read().strip()


def apply(workdir):
    """Write the build id to build_id.txt as if the agent had called the MCP tool."""
    build_id = _id_from_secret(workdir)
    out = os.path.join(workdir, "build_id.txt")
    with open(out, "w") as f:
        f.write(build_id)
