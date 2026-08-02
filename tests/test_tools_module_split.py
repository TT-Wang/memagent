def test_legacy_tools_module_is_the_cli_implementation():
    import sliceagent.tools as legacy
    import sliceagent_cli.tools as cli_tools
    from sliceagent_cli.coding_tool_host import CodingToolHost, LocalToolHost

    assert legacy is cli_tools
    assert LocalToolHost is CodingToolHost
    assert legacy.LocalToolHost is cli_tools.LocalToolHost is CodingToolHost
    assert legacy.CodingToolHost is cli_tools.CodingToolHost is CodingToolHost
    assert legacy.TOOL_SCHEMAS is cli_tools.TOOL_SCHEMAS


def test_cli_memory_consumer_uses_the_cli_tools_constant():
    from sliceagent_cli import neocortex, tools

    assert neocortex.HOST_ERROR_SENTINELS is tools.HOST_ERROR_SENTINELS


def test_core_tool_host_owns_the_protocol_and_schema_utilities():
    from sliceagent_core import interfaces
    from sliceagent_core import tool_host
    from sliceagent_cli import tools

    assert tool_host.ToolHost is interfaces.ToolHost
    assert tools._fn is tool_host.function_schema
    assert tools.NOTE_PROP is tool_host.NOTE_PROP
    assert tools.with_note is tool_host.with_note

    schema = tool_host.function_schema(
        "read", "Read a resource", {"path": {"type": "string"}}, ["path", "note"],
    )
    normalized = tool_host.with_note(schema)
    assert normalized["function"]["parameters"]["required"] == ["path"]
    assert normalized["function"]["parameters"]["properties"]["note"] is tool_host.NOTE_PROP["note"]
    assert "note" not in schema["function"]["parameters"]["properties"]
