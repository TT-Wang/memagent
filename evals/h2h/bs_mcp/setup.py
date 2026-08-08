import os
import secrets


# The build id is MINTED PER RUN (unguessable) and stashed in a secret dotfile the server reads at
# startup — it is NOT a literal in any readable source (mcp_server.py reads it from .specsvc/build_id.
# secret), so an agent cannot get it by read_file on the server source; the ONLY natural path is the MCP
# tool. (A determined agent could still read the dotfile — same bar as bs_plugin's .token — but the value
# is off the plain source.) The oracle spawns the same server and is anchored to whatever the tool returns.


# A minimal stdio MCP server. Primary path uses FastMCP (the high-level decorator
# API). If FastMCP is unavailable on the run host, it transparently falls back to
# the low-level mcp.server API exposing the same tool name + return value, so the
# scenario is robust to either SDK surface. Either way the server, when wired by
# the harness, surfaces a single tool `lookup_build_id` (namespaced by the client
# to `mcp__specsvc__lookup_build_id`) that returns MAGIC_BUILD_ID.
_SERVER_TMPL = '''\
"""specsvc — a minimal stdio MCP server exposing the project's build-id lookup.

The required build id is returned ONLY by the lookup_build_id tool below; it is
not stored in any readable file. An agent must call this tool (over MCP) to learn
the value. Run directly (stdio transport) by the MCP client the harness wires.
"""
import os
import sys

# The build id is NOT embedded in this source — it is read at startup from a per-run secret dotfile
# written by setup() next to this server. Reading THIS file does not reveal it; only calling the
# lookup_build_id tool over MCP returns it.
_SECRET = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".specsvc", "build_id.secret")
BUILD_ID = open(_SECRET, encoding="utf-8").read().strip()


def _run_fastmcp():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("specsvc")

    @mcp.tool()
    def lookup_build_id() -> str:
        """Return this project's required build id."""
        return BUILD_ID

    mcp.run()


def _run_lowlevel():
    # Fallback to the low-level mcp.server API if FastMCP is unavailable.
    import anyio
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    server = Server("specsvc")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name="lookup_build_id",
                description="Return this project's required build id.",
                inputSchema={{"type": "object", "properties": {{}}}},
            )
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name == "lookup_build_id":
            return [types.TextContent(type="text", text=BUILD_ID)]
        raise ValueError("unknown tool: %s" % name)

    async def _main():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(_main)


def main():
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401
    except Exception:
        _run_lowlevel()
        return
    _run_fastmcp()


if __name__ == "__main__":
    sys.exit(main())
'''


def setup(workdir):
    """Write the stdio MCP server that holds the magic build id.

    No other artifact carries the id — build_id.txt does not exist yet, and no
    file in the workdir contains MAGIC_BUILD_ID. The only way to obtain it is to
    call the specsvc MCP tool the harness wires from this server. (A leftover
    build_id.txt from a prior run is removed so a fresh setup is truly empty.)
    """
    # Mint a per-run, unguessable build id into a secret dotfile the server reads at startup — it is
    # NOT a literal in any readable source, so the only natural path to it is the MCP tool.
    build_id = "BLD-" + secrets.token_hex(3).upper()
    secret_dir = os.path.join(workdir, ".specsvc")
    os.makedirs(secret_dir, exist_ok=True)
    with open(os.path.join(secret_dir, "build_id.secret"), "w", encoding="utf-8") as f:
        f.write(build_id)

    server_path = os.path.join(workdir, "mcp_server.py")
    with open(server_path, "w") as f:
        f.write(_SERVER_TMPL.format())   # template carries no magic literal now (server reads the dotfile)

    # Ensure no stale answer is lying around from a previous run.
    out = os.path.join(workdir, "build_id.txt")
    if os.path.exists(out):
        os.remove(out)
