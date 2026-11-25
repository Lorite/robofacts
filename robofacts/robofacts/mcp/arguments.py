"""The MCP server's command-line arguments, kept apart from the server so that
``trials mcp --help`` works on a machine without the MCP Python SDK, as ``trials ui --help``
works without Flask."""
from __future__ import annotations

import argparse

from robofacts import roots

DESCRIPTION = ("an MCP server over stdio with read-only tools: validate, plan, dry run, "
               "coverage index, explain a trial. It never runs a trial. It needs the MCP "
               "Python SDK (pip install 'robofacts[mcp]').")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="trials mcp", description=DESCRIPTION)
    roots.add_root_arguments(ap, project=True)
    ap.add_argument("--list-tools", action="store_true",
                    help="print the tools and the project in force, then exit (no server)")
    return ap
