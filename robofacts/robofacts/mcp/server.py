"""The MCP server: the functions of ``queries.py`` as typed, read-only MCP tools over stdio.

Built on the official MCP Python SDK, version 2 (``mcp.server.mcpserver.MCPServer``, the
class that version 1 called FastMCP). Install it with ``pip install 'robofacts[mcp]'``.
"""
from __future__ import annotations

import copy
import inspect
from typing import Annotated, Any, Callable, Dict, List, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .. import __version__, roots
from . import queries

SERVER_NAME = "robofacts"

INSTRUCTIONS = """\
RoboFACTS plans and checks robot experiments that are declared in files: the factors, the \
metadata contract, the gate checklist and the campaigns of each experiment. This server reads \
the project in force and answers with JSON.

Every tool only reads or plans. The server never runs a trial, never runs a gate check against \
a robot, never writes the stop latch and never changes a file. Running trials stays with the \
operator at the command line. dry_run_campaign returns the exact command for that \
(operator_command, which ends in --execute). Do not run that command yourself: show it to the \
operator and let the operator decide.

A usual order: list_experiments, list_campaigns, validate_experiment (or lint for the whole \
project), plan_campaign, dry_run_campaign. After trials ran: coverage_index, list_trials and \
explain_trial (why one trial counts or does not).

Arguments are plain names, never paths: an experiment is the name of its folder, a campaign the \
name of its YAML file without .yaml, a trial the name of its recording folder. \
validate_experiment does not run the experiment's own test files (project code), the command \
`trials validate` does. The classes of a trial: usable (it counts), unverified (its metadata \
breaks the contract) and excluded (aborted, excluded by a person, no recorded data, or a \
scratch run). Prefer these tools to running `trials` and parsing its text."""

#: Every tool of this server carries these hints. The read-only test checks them. The tools
#: return the builtin dict[str, Any] (not typing.Dict), which the SDK serves as structured
#: content without wrapping it in {"result": ...}.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)

Experiment = Annotated[str, Field(description="the experiment's folder name, e.g. demo_log_recorder")]
Campaign = Annotated[str, Field(description="the campaign's file name without .yaml, e.g. grid")]
FactorLevels = Annotated[Optional[Dict[str, List[str]]], Field(
    description="factor levels that replace the campaign's, like the CLI's --set: "
                "{\"rate\": [\"slow\"]} pins a level, two or more levels sweep them")]
IncludeUnverified = Annotated[bool, Field(
    description="also count unverified trials (metadata that breaks the contract) as coverage")]


def _call(function: Callable[..., Dict[str, Any]], **kwargs) -> dict[str, Any]:
    """Run one tool with its printed output captured. A refusal reaches the caller as a tool
    error with its reason."""
    from .. import backend
    try:
        with queries.quiet():
            return function(**kwargs)
    except (queries.ToolInputError, roots.RootNotFound, backend.BackendError) as exc:
        raise ToolError(str(exc)) from exc


def list_experiments(
        with_counts: Annotated[bool, Field(description="count each experiment's trials by class")] = True,
) -> dict[str, Any]:
    """The project in force: its roots, its backend and its experiments with their campaigns and
    the number of usable, unverified and excluded trials."""
    return _call(queries.list_experiments, with_counts=with_counts)


def list_campaigns(experiment: Experiment) -> dict[str, Any]:
    """The campaigns of one experiment: what each sweeps and fixes, its repetitions, how many
    trials it wants, how many already count and how many are still needed."""
    return _call(queries.list_campaigns, experiment=experiment)


def validate_experiment(
        experiment: Experiment,
        campaign: Annotated[Optional[str], Field(description="validate only this campaign")] = None,
        skip: Annotated[Optional[List[str]], Field(
            description="stages to leave out: check, plan, gate, lint, tests, dry_run")] = None,
        verbose: Annotated[bool, Field(description="include the output of every stage")] = False,
) -> dict[str, Any]:
    """Is the experiment ready for its first trial? The stages of `trials validate`: every
    campaign loads (check), the planner accepts it (plan), the gate checklists are coherent
    (gate, nothing is checked against hardware), the declaration rules (lint) and a dry run per
    execution host. One verdict, `ready`. The experiment's own test files are not run here."""
    return _call(queries.validate_experiment, experiment=experiment, campaign=campaign, skip=skip,
                 verbose=verbose)


def lint(
        experiment: Annotated[Optional[str], Field(
            description="lint only this experiment (the rules over shared files still run)")] = None,
) -> dict[str, Any]:
    """The declaration rules of `trials lint` over the whole project or one experiment, with
    each failed rule and its message."""
    return _call(queries.lint, experiment=experiment)


def plan_campaign(experiment: Experiment, campaign: Campaign, factor_levels: FactorLevels = None,
                  include_unverified: IncludeUnverified = False) -> dict[str, Any]:
    """What a campaign still needs: per cell (one combination of levels) the trials wanted,
    the usable ones recorded, and the ones still missing, plus the order in which the runner
    would run them."""
    return _call(queries.plan_campaign, experiment=experiment, campaign=campaign,
                 factor_levels=factor_levels, include_unverified=include_unverified)


def dry_run_campaign(
        experiment: Experiment, campaign: Campaign, factor_levels: FactorLevels = None,
        include_unverified: IncludeUnverified = False,
        limit: Annotated[Optional[int], Field(description="at most this many trials", ge=1)] = None,
) -> dict[str, Any]:
    """The runner's dry run: for each trial the campaign still needs, the exact commands that
    would start the stack, record and run the trial. Nothing is launched, recorded or written.
    The result names the command the operator runs to execute the trials."""
    return _call(queries.dry_run_campaign, experiment=experiment, campaign=campaign,
                 factor_levels=factor_levels, include_unverified=include_unverified, limit=limit)


def coverage_index(
        experiment: Annotated[Optional[str], Field(description="one experiment, default all")] = None,
        factors: Annotated[Optional[List[str]], Field(
            description="the axes of the grid (default: the first three the contract declares)")] = None,
) -> dict[str, Any]:
    """The coverage index over the recorded trials: per cell the usable, unverified and excluded
    trials, the combinations with no trial, the trials that break the metadata contract and the
    excluded trials with their reasons."""
    return _call(queries.coverage_index, experiment=experiment, factors=factors)


def list_trials(
        experiment: Experiment,
        campaign: Annotated[Optional[str], Field(
            description="the trials this campaign ran or counts")] = None,
        status: Annotated[Optional[str], Field(
            description="usable, unverified, excluded, aborted, quarantined, no-data, scratch, "
                        "attempt (left no trial) or excluded-any")] = None,
        where: Annotated[Optional[Dict[str, str]], Field(
            description="factor levels every listed trial has, e.g. {\"rate\": \"fast\"}")] = None,
        limit: Annotated[int, Field(description="at most this many, newest first", ge=1)] = 100,
) -> dict[str, Any]:
    """The trials of one experiment, newest first (`trials records`): name, class, reason,
    campaign, factor levels and gate verdict. Attempts that left no trial are listed too."""
    return _call(queries.list_trials, experiment=experiment, campaign=campaign, status=status,
                 where=where, limit=limit)


def explain_trial(
        experiment: Experiment,
        trial: Annotated[str, Field(description="the trial's recording folder name, from list_trials")],
) -> dict[str, Any]:
    """Everything that decides whether one trial counts: its class and the reason, its recorded
    metadata, the contract problems, the gate verdict and the gate log, the verify steps, the
    attempt ledger and a person's decisions (exclusion, corrections, re-verification)."""
    return _call(queries.explain_trial, experiment=experiment, trial=trial)


TOOL_FUNCTIONS = {f.__name__: f for f in (
    list_experiments, list_campaigns, validate_experiment, lint, plan_campaign, dry_run_campaign,
    coverage_index, list_trials, explain_trial)}


class ReadOnlyServer(MCPServer):
    """The SDK's server, strict about arguments.

    The SDK drops an argument a tool does not declare. A call such as
    ``dry_run_campaign(..., execute=true)`` would then return a dry run, and the caller might
    believe that trials ran. Here it fails with the reason, and the input schemas say
    ``additionalProperties: false``."""

    async def list_tools(self):
        listed = await super().list_tools()
        for tool in listed:
            if tool.name in TOOL_FUNCTIONS:
                tool.input_schema = {**copy.deepcopy(tool.input_schema), "additionalProperties": False}
        return listed

    async def call_tool(self, name: str, arguments: Dict[str, Any], context=None):
        function = TOOL_FUNCTIONS.get(name)
        if function is not None:
            unknown = sorted(set(arguments or {}) - set(inspect.signature(function).parameters))
            if unknown:
                raise ToolError(
                    f"{name} takes no argument {', '.join(unknown)}. This server only reads and "
                    f"plans: no tool runs a trial, runs a gate check or changes a file. To run "
                    f"trials, show the operator the operator_command of dry_run_campaign.")
        return await super().call_tool(name, arguments, context)


def build_server() -> MCPServer:
    """The server with every tool of ``queries.READ_ONLY_TOOLS`` and nothing else."""
    assert tuple(TOOL_FUNCTIONS) == queries.READ_ONLY_TOOLS, "server.py and queries.py disagree"
    server = ReadOnlyServer(SERVER_NAME, title="RoboFACTS", version=__version__,
                       instructions=INSTRUCTIONS,
                       description="Read-only planning and coverage tools for declared robot "
                                   "experiments. Never runs a trial.")
    for name, function in TOOL_FUNCTIONS.items():
        server.add_tool(function, name=name, annotations=READ_ONLY, structured_output=True)
    return server


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(
        prog="trials mcp",
        description="an MCP server over stdio with read-only tools: validate, plan, dry run, "
                    "coverage index, explain a trial. It never runs a trial.")
    roots.add_root_arguments(ap, project=True)
    ap.add_argument("--list-tools", action="store_true",
                    help="print the tools and the project in force, then exit (no server)")
    args = ap.parse_args(argv)
    # Explicit roots go to the environment, so every module of the package agrees on them.
    if args.experiments_root:
        os.environ[roots.ENV_EXPERIMENTS_ROOT] = str(args.experiments_root.expanduser().resolve())
    if args.project_root:
        os.environ[roots.ENV_PROJECT_ROOT] = str(args.project_root.expanduser().resolve())
    server = build_server()
    if args.list_tools:
        try:
            where = f"experiments root: {roots.experiments_root()}"
        except roots.RootNotFound as exc:
            where = f"no project in force yet ({exc})"
        print(f"robofacts MCP server {__version__}, {where}")
        for name in queries.READ_ONLY_TOOLS:
            doc = (TOOL_FUNCTIONS[name].__doc__ or "").strip().splitlines()[0]
            print(f"  {name:<20} read-only  {doc}")
        return 0
    print(f"robofacts MCP server {__version__} on stdio (read-only, never runs a trial)",
          file=sys.stderr)
    server.run("stdio")
    return 0
