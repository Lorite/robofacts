# The MCP server for agents

`trials mcp` starts a [Model Context Protocol](https://modelcontextprotocol.io) server over stdio. An agent, such as Claude Code, then validates, plans and dry-runs campaigns and reads the coverage index through typed tools with JSON results, instead of running `trials` and parsing its text.

## Install

The server needs the MCP Python SDK (version 2), which an optional extra installs:

```bash
pip install 'robofacts[mcp]'
```

From a clone of the repository, the same extra is `pip install './robofacts[mcp]'`. The other commands of `trials` do not need it.

## The server only reads and plans

No tool runs a trial, runs a gate check against a robot, writes the stop latch or changes a file. The dry run returns the exact `trials run ... --execute` command, and the operator decides whether to run it at the command line. The server's instructions tell the agent the same.

A test of the package holds this boundary. It calls every tool on a copy of the example project with recorded trials, and it fails when a file changed, a process other than the lint's pytest started, a socket opened, or the stop latch was written. A static check of the server's code fails when the code names a path that executes something.

## The tools

| Tool | Answers |
| --- | --- |
| `list_experiments` | the project in force (roots, backend) and its experiments, with their campaigns and their trials per class |
| `list_campaigns` | per campaign of an experiment: the sweep, the fixed levels, the repetitions, and the trials wanted, covered and still needed |
| `validate_experiment` | the stages of `trials validate` (check, plan, gate, lint, dry run) and one verdict |
| `lint` | the declaration rules over the project or one experiment, each failure with its rule and subject |
| `plan_campaign` | the cells of a campaign with the trials wanted, usable and still missing, and the order the runner would run them in. `factor_levels` replaces levels like `--set` |
| `dry_run_campaign` | the exact commands of the next trials (stack, recorder, action), with nothing executed, and the command that the operator runs to execute them |
| `coverage_index` | the usable, unverified and excluded trials per cell, the missing combinations, the trials that break the metadata contract, and the reasons of the excluded ones |
| `list_trials` | the trials of an experiment, newest first, with class, reason, campaign, levels and gate verdict |
| `explain_trial` | why one trial counts or not: its metadata, the contract problems, the gate verdict and the gate log, the verify steps, the attempt ledger and a person's decisions |

`validate_experiment` leaves out the experiment's own test files, because they are project code, so the server does not run them. `trials validate` at the command line runs them.

## Arguments are names, never paths

Every argument names something inside the project: the experiment's folder, the campaign's file name without `.yaml`, the trial's recording folder. So every read stays inside the experiments root. A tool refuses an argument that it does not declare: `dry_run_campaign` with `execute: true` fails with the reason, instead of returning a dry run that someone could take for a run.

## Connect it to Claude Code

The server finds the project as every `trials` command does (see [[projects]]), from the directory where the client starts it. To add it from the project directory:

```bash
claude mcp add robofacts -- trials mcp
```

`claude mcp add -s project` writes the same server into the project's `.mcp.json`, to share it with everyone who works in the repository. With an explicit experiments root, the file reads:

```json
{
  "mcpServers": {
    "robofacts": {
      "type": "stdio",
      "command": "trials",
      "args": ["mcp", "--experiments-root", "/path/to/project/experiments"],
      "env": {}
    }
  }
}
```

`trials mcp` also accepts `--project-root`.

## Check the configuration

`trials mcp --list-tools` prints the tools and the experiments root that the server resolves, and then exits without starting a server. It is the fastest check of a configuration.

On a ROS 2 project, the server must import `robofacts_ros2` like every other command, so the client must start it from an environment where the ROS 2 workspace is sourced. See [[ros2-backend]].

## What stays with the operator

An agent prepares, and a person executes. A typical session: the agent calls `validate_experiment` until READY, `plan_campaign` to see the missing cells, and `dry_run_campaign` to get the commands. The operator reads them and runs the `--execute` command in a terminal. For a trial with a real robot, the rules of [[hardware-runs]] apply, and the pose pause waits for a person to type `fly`.
