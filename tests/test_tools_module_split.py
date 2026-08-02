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
