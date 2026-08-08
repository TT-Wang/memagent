import os
import sys
import asyncio


# Independent oracle. We do NOT hardcode the expected build id and we do NOT trust
# any file the agent could have fabricated. Instead we start the SAME stdio MCP
# server that setup() wrote, in a fresh subprocess, call its lookup_build_id tool
# over the real MCP protocol, and require build_id.txt to equal — exactly — the
# value the live tool returns. So the only way build_id.txt can match is if the
# agent obtained the id THROUGH the MCP mechanism (the magic value lives nowhere
# else). A no-op leaves build_id.txt absent → FAIL.


async def _tool_value(workdir):
    """Spawn the scenario's stdio MCP server and return what lookup_build_id gives."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["mcp_server.py"],
        cwd=workdir,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            if "lookup_build_id" not in names:
                raise RuntimeError(
                    "specsvc server did not expose lookup_build_id (tools=%r)" % names
                )
            res = await session.call_tool("lookup_build_id", {})
            parts = []
            for block in (res.content or []):
                t = getattr(block, "text", None)
                if t is not None:
                    parts.append(t)
            return "".join(parts).strip()


def verify(workdir):
    server = os.path.join(workdir, "mcp_server.py")
    if not os.path.isfile(server):
        return False, "mcp_server.py not found in workdir (setup did not run)"

    out = os.path.join(workdir, "build_id.txt")
    if not os.path.isfile(out):
        return False, "build_id.txt not written (agent never produced the build id)"

    with open(out, "r") as f:
        written = f.read().strip()
    if not written:
        return False, "build_id.txt is empty"

    # Ask the live MCP tool what the correct value is (anchored to the mechanism,
    # not to a hardcoded literal in this oracle).
    try:
        expected = asyncio.run(asyncio.wait_for(_tool_value(workdir), timeout=30))
    except Exception as e:  # noqa: BLE001
        return False, "could not query specsvc MCP tool to establish oracle: %r" % (e,)

    if not expected:
        return False, "specsvc MCP tool returned an empty build id (oracle broken)"

    if written != expected:
        return False, (
            "build_id.txt=%r but the specsvc MCP tool returns %r — the agent did "
            "not obtain the id through the MCP tool" % (written, expected)
        )

    return True, (
        "build_id.txt matches the live specsvc MCP tool return value %r "
        "(the magic id obtainable only via the MCP mechanism)" % (expected,)
    )
