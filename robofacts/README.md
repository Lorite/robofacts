# RoboFACTS

RoboFACTS is a framework for experiments with robots, in simulation and on hardware. You declare an experiment once (its factors, the metadata every trial must record, the checks before each trial, how to run and verify one trial), and the framework plans the trials, runs them, checks each recording, and counts only the trials that satisfy the declaration. The same declaration drives one robot or a team, on one computer or several.

The name stands for Factors, A Contract, and checked Trials in Simulation and on hardware. The framework is described in a paper accepted at ACM SAC 2027 (see [Cite](#cite)).

This is the general package. It needs Python 3.10 or newer, PyYAML and pytest (the linter runs its rules with it), and no robot middleware. Its built-in backend, `process`, starts the system under test as a shell command and records the files that the system writes during a trial. A backend for a robot middleware is a separate package: the [repository](https://github.com/Lorite/robofacts#two-packages) holds one, built from source.

## Install

```bash
pip install robofacts            # the web interface (trials ui) also needs Flask: pip install "robofacts[ui]"
trials --help
```

## Your own project in five steps

```bash
trials init my_project --experiment my_experiment     # trials.yaml, the shared config, one experiment
cd my_project
trials lint                                            # every declaration against the rules
trials plan experiments/my_experiment/campaigns/baseline.yaml
trials run  experiments/my_experiment/campaigns/baseline.yaml --limit 1    # dry run: the exact commands
trials run  experiments/my_experiment/campaigns/baseline.yaml --execute --no-pause    # the trials
```

`--no-pause` skips the stop before each trial. Without it, every trial waits so that you can pose the rig: for Enter in a simulation, and for the word `fly` on real hardware. A hardware run therefore needs a terminal, or `trials run` refuses it unless you pass `--no-pause`. A hardware trial also runs only after a gate, so its experiment needs a gate checklist.

The generated experiment works as it is: a small script stands in for the system and writes a log file, the recorder stores what it writes during a trial, and a verify step reads the recording. `trials index --experiment my_experiment --summary` then lists the usable trials. Edit the declarations in `experiments/my_experiment/config/` to describe your own system, and `trials validate experiments/my_experiment` says whether it is ready for a first trial.

## An MCP server for agents

`trials mcp` starts a Model Context Protocol server for agents such as Claude Code. Its tools only read and plan: they validate, plan, dry-run and read the coverage index, and they never run a trial. Install it with `pip install 'robofacts[mcp]'`. The tools are listed in [the repository README](https://github.com/Lorite/robofacts#an-mcp-server-for-agents).

## Documentation and examples

- [Documentation](https://lorite.github.io/robofacts/): install, quickstart, the concepts, guides, and a reference of every command, lint rule, gate check and declaration file
- [Examples](https://lorite.github.io/robofacts/getting-started/examples/): a log recorder, a middleware demo, and one small drone that hovers without any middleware
- [Changelog](https://github.com/Lorite/robofacts/blob/main/CHANGELOG.md)

## Cite

If you use RoboFACTS, please cite the paper:

> Alejandro Lorite-Mora. RoboFACTS: A Declarative Experiment Design Framework for Multi-Robot Trials in Simulation and on Hardware. In Proceedings of the 42nd ACM/SIGAPP Symposium on Applied Computing (SAC '27), Gwangju, South Korea, April 5 to 9, 2027.

## License

Apache License 2.0.
