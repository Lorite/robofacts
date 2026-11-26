# Projects

A project is one directory tree that holds your experiments and their shared configuration. This page shows its layout, how every `trials` command finds it, and what `trials.yaml` sets.

## The layout

```text
my_project/
  trials.yaml                    the project file: the two roots, the backend, the settings
  experiments/                   the experiments root
    common/
      config/                    the configuration every experiment shares
        factors.yaml             the core factors: execution_host, record_profile
        trial_metadata_core.yaml the core of the metadata contract
        record_profiles.yaml     the named sets of data a trial can record
        robots.yaml              each robot's stop commands and reset steps
        storage.yaml             where each host writes heavy recordings
      preflight/checks/          the project's own gate checks (optional)
    my_experiment/               one experiment
```

`trials init <directory>` creates this tree, and `--experiment <name>` adds a first experiment (see [[first-project|Your first project]]). [[experiments|Experiments]] describes the folder of one experiment.

The **experiments root** is the folder that holds `common/` and the experiments. The **project root** is the working directory of every command that a `runner.yaml` declares, which is why the commands in the examples start with paths such as `experiments/<name>/scripts/`. It is also the checkout whose code version the runner records with every trial.

## How a command finds the project

Every `trials` command resolves the experiments root in this order, and the first answer wins:

1. the `--experiments-root` option,
2. the environment variable `TRIALS_EXPERIMENTS_ROOT`,
3. a `trials.yaml` found by walking up from the current directory, with its key `experiments_root:` (a relative path is relative to that file),
4. walking up from the current directory: the first directory that holds `common/config/trial_metadata_core.yaml`, or that has a subdirectory `experiments/` which holds it.

When nothing answers, the command stops with an error that says how to set the root.

The project root resolves the same way: the `--project-root` option (on the commands that take it), the environment variable `TRIALS_PROJECT_ROOT`, the key `project_root:` of `trials.yaml`, and else the parent of the experiments root.

So you can run every command from anywhere inside the project. `trials roots` prints the result:

```text
experiments root : /home/you/my_project/experiments
project root     : /home/you/my_project
trials.yaml      : /home/you/my_project/trials.yaml
environment      : TRIALS_EXPERIMENTS_ROOT= TRIALS_PROJECT_ROOT=
```

!!! note
    The project settings belong to the project whose experiments are in force, not to the project around your working directory. When you set `TRIALS_EXPERIMENTS_ROOT`, the settings come from the `trials.yaml` of that project. When you set `TRIALS_PROJECT_ROOT` to a folder that holds a `trials.yaml`, that file wins.

## Project settings in trials.yaml

`trials init` writes this file:

```yaml
experiments_root: experiments
project_root: .
backend: process
```

`backend:` is required. It names the [[backends|backend]]: `process` is built in, and another name loads the package `robofacts_<name>`. Without the line, every command stops and says which line to add, because a guess could read a project with the wrong recorder. The environment variable `TRIALS_BACKEND` overrides the line for one session.

Besides the two roots and the backend, `trials.yaml` holds the names a project gives to what would otherwise be constants in the package. So the package itself knows no robot. Common keys:

| Key | What it sets |
| --- | --- |
| `stop_latch` | the file every stop command writes first. The gate check `stop_latch` refuses a trial while this file is younger than 120 s. Default `/tmp/trial_stop_latch` |
| `default_record_profile` | the record profile of a trial without a `record_profile` factor. Without it, the runner refuses such a trial |
| `record_profiles` | the names that fill the placeholders of the project's record profiles |
| `parallel_safe_checks` | the project's own gate checks that only read, so that the gate may run them together |
| `stop_patterns` | what the stop sweeps must also match |

[[trials-yaml|trials.yaml]] lists every key. The commented `trials.yaml` that `trials init` writes also lists the optional keys of its backend.

## Environment variables

Every environment variable that the package reads starts with `TRIALS_`. These concern the project:

- `TRIALS_EXPERIMENTS_ROOT` and `TRIALS_PROJECT_ROOT`: the two roots.
- `TRIALS_BACKEND`: the backend for one session, instead of the `backend:` line.
- `TRIALS_STOP_LATCH`: another path for the stop latch, made for tests without hardware.
- `TRIALS_BY`: who decided, when a command records [[decisions|a person's decision about a trial]].

The others belong to one feature: the gate's concurrency (see [[gate|The T-0 gate]]), the spoken cues (see [[spoken-cues|Spoken operator cues]]) and the robot registry (see [[robots-and-hosts|robots.yaml and hosts.yaml]]).

## With the ROS 2 backend

A ROS 2 project says `backend: ros2`. Its experiments keep their recordings as bags in `bags/` instead of `recordings/`, and its `trials.yaml` may also enable the robot adapters it uses (`adapters:`) and name the colcon workspaces whose code version each trial records. See [[ros2-backend|The ROS 2 backend]] and [[robot-adapters|Robot adapters]].
