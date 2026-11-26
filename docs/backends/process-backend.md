# The process backend

The process backend is built into the `robofacts` package and works without any middleware. A shell command starts the stack, and a small recorder stores what the system writes to its log files during the trial. This page shows how an experiment declares both, and what a recording holds.

## When to use it

- Your system under test is a set of processes that write log files.
- You want to learn the framework, or test a project, without ROS 2.

It is also the second implementation of the [[backend-contract|backend interfaces]], so the tests of the package exercise the whole trial loop without any middleware. A project selects it in `trials.yaml`:

```yaml
backend: process
```

`trials init` writes this line by default.

## Declare the stack and the sources

This excerpt of a `runner.yaml` comes from the example project of the package:

```yaml
simulation: true

# The stack: one command that runs until the runner stops it.
launch:
  command: "python3 experiments/demo_log_recorder/scripts/system.py --rate {rate} --log experiments/demo_log_recorder/runs/system.log"

# What to record: the files whose new lines the recorder stores during the trial.
record_sources: [experiments/demo_log_recorder/runs/system.log]

# The one foreground command that is the trial.
action:
  command: "python3 experiments/demo_log_recorder/scripts/run_trial.py --duration {duration}"

verify_steps:
  - name: log_was_recorded
    command: "python3 experiments/demo_log_recorder/scripts/verify_log.py {recording} --rate {rate} --min-lines 3"
```

- `launch: {command: "..."}` is optional. An experiment without `launch:` has no stack, and the action is the whole trial.
- The launch command is templated like the action: `{factor}` for a factor level and `{arg_<name>}` for a launch argument. Each value stays one argument, also when it holds a space.
- `record_sources` lists the files to follow. `record_topics` is the same key under its older name. A trial may also take its sources from a [[record-profiles|record profile]].
- A command names the trial's recording directory as `{recording}`, or `{bag}`.

The backend refuses `launch.package` and `launch.file`, which belong to another backend, and it names the line to add to `trials.yaml`.

## What a recording holds

A recording is a directory:

```text
recordings/<trial>/
  data/system.log       # the bytes appended to each source while the recorder ran
  recording.json        # written last: the start, the stop and the bytes per source
  trial_metadata.json   # written by the runner after the trial
```

The recorder follows each source until SIGINT or SIGTERM:

- A file that exists when the recorder starts is followed from its end, so the recording holds this trial's part of a log and not the history before it.
- A file that appears later is taken from its first byte.
- A file that is replaced by a shorter one is read again from its start.
- Two sources with the same file name get unique names in `data/`.

On the stop, the recorder copies what is left and writes `recording.json` last, through a temporary file and a rename. So the manifest is either complete or absent.

## When a recording counts

The runner asks the recorder whether the recording is complete, on the host that recorded it. A recording is complete when `recording.json` exists and at least one file in `data/` is not empty. Otherwise the trial fails and the runner writes no metadata, so a trial during which the system wrote nothing can never fill a cell. A recording without `recording.json` is not finalized, and the storage tools do not move it.

## How it runs

- The recorder runs with the Python interpreter that runs `trials`, when that interpreter exists on the machine of the trial. On another host it runs with `python3`, which must import the installed package there.
- The backend has no stop patterns. The runner stops the process group that it started, which is the whole stack.
- The folders of an experiment are `recordings/`, `recordings_issues/`, `recordings_slim/`, `recordings_issues_slim/`, `recordings_scratch/` and `recordings_scratch_slim/`.

## What it does not have

The process backend ships no gate checks beyond the 8 general ones, no [[robot-adapters]], no slim copy, no extra commands and no Explore. The [[web-ui|web interface]] works on it and shows the plan, the coverage grid, the trials, the gate and its confirmations, the runs and the journal. For a ROS 2 stack, use the [[ros2-backend|ROS 2 backend]]. For another middleware, write a backend of your own with [[backend-contract]].
