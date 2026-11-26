# Backends

A backend connects RoboFACTS to the middleware of the system under test. This page explains what a backend does, how a project chooses one, and how the two shipped backends differ.

## What depends on the middleware

The planner, the linter, the gate protocol, the trial loop and the coverage index do not depend on the middleware. Two things do: starting the stack, and recording the trial. A backend supplies both, as a launcher and a recorder that return command lines. The [[runner]] keeps all process handling: the background start, the ssh and container wrapper, process groups, the signal sequence and the run log. [[backend-contract]] describes the interfaces.

A backend may also ship more: the gate checks of its middleware with their spoken sentences, [[robot-adapters]], lint rules, an experiment template, extra `trials` commands, shell tools, and pages of its own in the [[web-ui|web interface]].

## Choose a backend

Every project names its backend in `trials.yaml`:

```yaml
backend: process    # built in: a shell command, and a recorder that stores what the system appends to its log files
backend: ros2       # the package robofacts_ros2: ros2 launch or ros2 run, ros2 bag record, a recording is a bag
```

There is no default. Without the line, every command stops and says which line to add, because a guess could read a project with the wrong recorder, and every recording would then look empty. `trials init` writes the line (`--backend`, default `process`).

A name loads the package `robofacts_<name>`, and a dotted name loads any importable module with a `BACKEND` object. `$TRIALS_BACKEND` overrides the file for one session.

## The two shipped backends

| | [[process-backend|process]] | [[ros2-backend|ros2]] |
| --- | --- | --- |
| Package | built into `robofacts` | `robofacts_ros2` |
| Needs | Python only | ROS 2 Humble |
| Stack | `launch: {command: "..."}` in `runner.yaml`, optional | `launch: {package, file}`, with `ros2 launch` or `ros2 run` |
| Recording | a directory with `data/` and `recording.json` | a rosbag2 directory, a bag |
| What it records | the lines that the system appends to its files | topics |
| Gate checks | the 8 general checks | 13 more, and those of each adapter |
| Robot adapters | none | three |
| Extra commands | none | `trials explore`, `trials slim`, a ROS 2 `trials profiles` |
| Extra tools | none | `kill_ros.sh` |
| Web interface | plan, coverage grid, trials, gate, runs, journal | adds replay in Foxglove, the Viewing page, bag download, Kill ROS, Explore |

Use the process backend when your system is a set of processes and log files, or to learn the framework without any middleware. Use the ROS 2 backend for a ROS 2 stack. For another middleware, write a backend of your own: [[backend-contract]] lists what it must provide.

A `runner.yaml` with `launch.package` and `launch.file` is refused on the process backend, with the line to add to `trials.yaml`. So a ROS 2 project that forgot the `backend:` key cannot run a trial without its stack.

## The folders of an experiment

The backend names the folders where an experiment keeps its recordings:

| Role | process | ros2 |
| --- | --- | --- |
| counted trials | `recordings/` | `bags/` |
| excluded trials | `recordings_issues/` | `bags_issues/` |
| slim copies | `recordings_slim/` | `bags_slim/` |
| slim copies of excluded trials | `recordings_issues_slim/` | `bags_issues_slim/` |
| scratch trials | `recordings_scratch/` | `bags_scratch/` |
| slim copies of scratch trials | `recordings_scratch_slim/` | `bags_scratch_slim/` |

The ROS 2 backend keeps the older names, so its projects and their data stay as they are.

## Older names that stay

Some names are older than the backends and come from the ROS 2 word "bag". They work on every backend, because recorded files already hold them:

- the option `--bags-root`, also spelled `--recordings-root`
- the gate check `bag_dir_writable`, also named `recording_dir_writable`
- the field `bag_storage` in `trial_metadata.json`, and `bag_dir` in `runs/attempts.jsonl`
- the placeholder `{bag}` in a command, also spelled `{recording}`
- the key `record_topics` in `runner.yaml`, also spelled `record_sources`
