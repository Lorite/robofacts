"""An MCP (Model Context Protocol) server for robofacts, so that an agent can validate, plan
and dry-run campaigns and read the coverage index with typed tools and JSON results.

The server only reads and plans. It never runs a trial, never runs a gate check against a
robot, never writes the stop latch and never changes a file: execution stays with the
operator at the command line. ``trials mcp`` starts it over stdio. It needs the optional
extra: ``pip install 'robofacts[mcp]'``.

``queries`` holds the functions and does not import the MCP SDK. ``server`` registers them
as MCP tools.
"""
