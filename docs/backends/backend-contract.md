# What a backend provides

This page lists what a [[backends|backend]] must provide and what it may add, for anyone who connects RoboFACTS to another middleware. The interfaces live in `robofacts/backend.py`.

## The shape of a backend

A backend is a Python module named `robofacts_<name>`, or any dotted module path that `backend:` in `trials.yaml` names. The module holds one object, `BACKEND`, an instance of `robofacts.backend.Backend`.

The runner keeps everything that handles processes: the background start, the ssh and container wrapper, process groups, the signal sequence and the run log. So a backend never starts or stops a process itself. It returns command lines.

## Required: a launcher

A launcher (`robofacts.backend.Launcher`) starts the stack of one trial.

| Method | Returns |
| --- | --- |
| `parse(raw, path)` | the stack declaration of one `runner.yaml` (`raw` is its parsed content), validated. It raises `BackendError` with the reason |
| `command(stack, args, values)` | the command line that starts the stack and runs until it is stopped, or `None` for an experiment without a stack. `args` are the trial's launch arguments, and `values` its factor levels |
| `stop_patterns()` | process patterns per kind (`all`, `recorder`, `launch`) for the stop sweeps, regular expressions with a `{workspace}` placeholder. Empty when stopping the process group is enough |

## Required: a recorder

A recorder (`robofacts.backend.Recorder`) records one trial into one directory.

| Member | Returns |
| --- | --- |
| `noun` | the word for one recording in messages and pages, for example "recording" or "bag" |
| `command(directory, sources, options)` | the command line that records `sources` into `directory` until SIGINT, and then finalizes the recording |
| `has_data(target, directory)` | `(ok, reason)`: is the recording complete? The runner asks on the host that recorded it (`target` is `None` for this machine). Without a complete recording the runner writes no metadata, so the trial never counts |
| `holds_data(directory)` | does a local trial directory hold recorded data? The coverage index asks it of every trial, so it must be cheap |
| `unsettled_reason(directory)` | why a local recording is not finalized yet, or `None`. The storage tools ask it before they move a recording |
| `after_trial(directory, target, hooks, project_root, reduced=True)` | optional work on a finished recording, such as a text export or a reduced copy. It must never raise, because the trial has happened |

## Optional fields

Each field of `Backend` beyond the two interfaces has a working default:

| Field | Holds |
| --- | --- |
| `host_setup` | the two setup files that a command sources on a remote host, from the host's settings |
| `gate_prepare` | a step before the gate's machine checks, for example a restart of a middleware daemon |
| `checks_dir`, `checks_lib` | the gate checks of the middleware, and the bash helper they source |
| `parallel_safe_checks`, `sequential_checks` | which of those checks may share a batch, and which must run alone (with the reason) |
| `gate_sentences` | the spoken sentence of each failed check |
| `adapters_package` | the package that holds the [[robot-adapters]] |
| `record_dirs` | the folder names of an experiment, by default `recordings*` |
| `features` | optional parts by name: `explore`, `trial_media`, `slim`, and `ui` (an extension of the web interface) |
| `commands` | extra `trials` subcommands |
| `lint_files` | lint rules of the middleware, as pytest files |
| `skeleton_dir` | templates for `trials new` and `trials init` |
| `tools_dir`, `clean_slate_tool` | shell tools for `trials tool`, and the one that stops every process of the backend's stacks |
| `cli`, `package` | how `trials` and the package's modules start on another machine |
| `build_workspaces` | the build workspaces whose state the code version of a trial records |

## A minimal backend

This module is a complete backend. Its stack is one shell command, and its recorder is a tool `my-recorder` that writes `.dat` files and a file `done` when it finalizes:

```python
"""robofacts_mylog: a backend whose recorder is the tool my-recorder."""
from pathlib import Path

from robofacts.backend import Backend, BackendError


class MyLauncher:
    def parse(self, raw, path):
        launch = raw.get("launch") or {}
        if not launch.get("command"):
            raise BackendError(f"{path}: launch.command is required")
        return {"command": str(launch["command"])}

    def command(self, stack, args, values):
        return ["sh", "-c", stack["command"]] if stack else None

    def stop_patterns(self):
        return {}   # the runner stops the process group it started


class MyRecorder:
    noun = "recording"

    def command(self, directory, sources, options):
        return ["my-recorder", "--out", str(directory), *sources]

    def has_data(self, target, directory):
        if target is not None:
            return False, "this recorder cannot check another host"
        if not (Path(directory) / "done").is_file():
            return False, "the recorder never finalized the recording"
        return True, ""

    def holds_data(self, directory):
        return any(Path(directory).glob("*.dat"))

    def unsettled_reason(self, directory):
        return None if (Path(directory) / "done").is_file() else "no done file"

    def after_trial(self, directory, target, hooks, project_root, *, reduced=True):
        return None


BACKEND = Backend(name="mylog", launcher=MyLauncher(), recorder=MyRecorder())
```

With the package on the Python path, a project selects it with `backend: mylog`.

## Rules that keep a backend honest

- Return a complete recording or say why not. `has_data` decides whether a trial can count at all, so it must check the files that the recorder writes last.
- Finalize on SIGINT. The runner stops the recorder before the stack, so the recording is complete before the stack goes down.
- Never raise from `after_trial`. Print what failed and return.
- Name no robot and no lab outside the robot adapters. A test of the repository fails the build when a file of either package does.

## The two shipped implementations

`robofacts/backends/process.py` is about 130 lines: a shell command as launcher, and a recorder that stores what the system appends to its log files. `robofacts_ros2/__init__.py` with its `launcher.py` and `recorder.py` is the ROS 2 backend, with 13 gate checks, 3 robot adapters, Explore and the ROS 2 part of the web interface. Read them as examples. A test of the package runs whole trials through the interfaces with real processes on the process backend, and another checks the gate, the override, the verify steps and the coverage index on it.
