"""The MCP server around the queries: its tools, their read-only hints, its instructions, its
refusal of unknown arguments, and one session over stdio through ``trials mcp``.

Needs the MCP Python SDK (``pip install 'robofacts[mcp]'``). Skipped where it is not
installed. The read-only boundary of the queries themselves is in ``test_mcp_read_only.py``,
which needs no SDK.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

from robofacts import cli
from robofacts.mcp import queries

# A guarded import and a skip mark, not a module-level pytest.importorskip: in a ROS 2
# environment (its launch_testing pytest plugins, a project's pytest.ini) a Skipped raised
# while this module is imported skipped the collection of EVERY core test, silently
# (2026-11-25, the dev container collected 0 core tests).
try:
    import anyio  # installed with the SDK
    from mcp import Client, StdioServerParameters

    from robofacts.mcp import server
except ImportError:
    server = None

pytestmark = pytest.mark.skipif(server is None, reason="the MCP Python SDK 2 is not installed "
                                                       "(pip install 'robofacts[mcp]')")

PACKAGE = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE / "example_project"
#: Argument names that would mean a tool can act. No tool may take one.
ACTING_ARGUMENT = re.compile(r"execute|override|scratch|force|write|delete|remove|exclude|amend|"
                             r"restore|latch|estop|stop|command|action|run|path|root", re.IGNORECASE)


def session(check):
    """Run ``check(client)`` against the server in this process."""
    async def main():
        async with Client(server.build_server()) as client:
            return await check(client)
    return anyio.run(main)


def test_the_server_lists_exactly_the_read_only_tools_with_read_only_hints():
    async def check(client):
        return (await client.list_tools()).tools
    tools = session(check)
    assert [t.name for t in tools] == list(queries.READ_ONLY_TOOLS)
    for tool in tools:
        hints = tool.annotations
        assert hints.read_only_hint is True and hints.destructive_hint is False, tool.name
        assert hints.open_world_hint is False and hints.idempotent_hint is True, tool.name
        assert tool.input_schema.get("additionalProperties") is False, tool.name
        acting = [name for name in tool.input_schema.get("properties", {}) if ACTING_ARGUMENT.search(name)]
        assert not acting, f"{tool.name} takes an argument that sounds like acting: {acting}"
        assert tool.description and tool.output_schema["type"] == "object"


def test_the_instructions_state_the_boundary():
    async def check(client):
        return client.instructions
    text = session(check)
    for phrase in ("never runs a trial", "never runs a gate check", "never writes the stop latch",
                   "never changes a file", "operator", "Do not run that command yourself"):
        assert phrase in text, phrase


def test_an_unknown_argument_is_refused_not_ignored():
    """The SDK would drop ``execute`` and return a dry run, and the caller could believe that
    trials ran. The server refuses it with the reason."""
    async def check(client):
        return await client.call_tool("dry_run_campaign",
                                      {"experiment": "demo_log_recorder", "campaign": "grid",
                                       "execute": True})
    result = session(check)
    assert result.is_error
    text = result.content[0].text
    assert "takes no argument execute" in text and "only reads and plans" in text


def test_a_tool_answers_with_structured_content_and_a_refusal_with_its_reason():
    async def check(client):
        plan = await client.call_tool("plan_campaign",
                                      {"experiment": "demo_log_recorder", "campaign": "grid"})
        missing = await client.call_tool("plan_campaign",
                                         {"experiment": "demo_log_recorder", "campaign": "nope"})
        return plan, missing
    plan, missing = session(check)
    assert not plan.is_error
    assert plan.structured_content["wanted"] == 4 and "cells" in plan.structured_content
    assert missing.is_error and "no campaign 'nope'" in missing.content[0].text


def test_trials_mcp_list_tools_prints_every_tool(capsys):
    assert cli.main(["mcp", "--list-tools"]) == 0
    out = capsys.readouterr().out
    assert "experiments root:" in out
    for name in queries.READ_ONLY_TOOLS:
        assert re.search(rf"^\s+{name}\s+read-only", out, re.MULTILINE), name


def test_trials_mcp_serves_over_stdio(tmp_path):
    """The command a client configures, as a real process: `python -m robofacts.cli mcp`."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "robofacts.cli", "mcp", "--experiments-root", str(EXAMPLE / "experiments")],
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(PACKAGE) + os.pathsep + os.environ.get("PYTHONPATH", ""),
             "TRIALS_VOICE": "off"},
    )

    async def main():
        async with Client(params) as client:
            tools = (await client.list_tools()).tools
            listed = await client.call_tool("list_experiments", {"with_counts": False})
            return [t.name for t in tools], listed
    names, listed = anyio.run(main)
    assert names == list(queries.READ_ONLY_TOOLS)
    assert not listed.is_error
    assert listed.structured_content["experiments_root"] == str(EXAMPLE / "experiments")
    assert [e["name"] for e in listed.structured_content["experiments"]] == ["demo_log_recorder"]
