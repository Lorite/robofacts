# trials.yaml

`trials.yaml` is the project file. This page lists every key that the code reads from it, with its type, its default and its meaning.

## Where the file is

The file sits at the root of a [[projects|project]], and `trials init` writes it. A `trials` command finds it by walking up from the working directory, so every command works anywhere below the project root. When `$TRIALS_PROJECT_ROOT` names a directory that holds a `trials.yaml`, that file is used instead. `trials roots` prints which file is in force and where the two roots resolve.

## A minimal example

```yaml
experiments_root: experiments
project_root: .
backend: process
```

The optional settings go into the same file. An example with some of them:

```yaml
backend: process
stop_latch: /tmp/trial_stop_latch
default_record_profile: logs
parallel_safe_checks: [my_check]
stop_patterns:
  kinds: {my_kind: "my_daemon.*{workspace}"}
record_profiles:
  log_dir: /var/log/my_system
```

## Keys

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `backend` | string | yes | none | The [[backends|backend]] of the project: `process` (built in), another name such as `ros2` (the package `robofacts_<name>`), or a dotted module path that holds a `BACKEND` object. There is no default: a project without this key stops with an error. |
| `experiments_root` | path | no | found by the layout | The directory that holds the experiments, relative to this file. Without the key, the command uses the nearest directory that holds `common/config/trial_metadata_core.yaml`, or that has an `experiments/` directory with that file. |
| `project_root` | path | no | the parent of the experiments root | The working directory of the commands of `runner.yaml`, and the checkout whose code version every trial records. Relative to this file. |
| `stop_latch` | path | no | `/tmp/trial_stop_latch` | The file that every stop command writes first. The `stop_latch` gate check refuses a trial while this file is younger than 120 s. |
| `build_workspaces` | list of paths | no | the backend's list | The build workspaces, relative to the checkout. The code version of a trial says for each one whether it was built after its last commit. A single string counts as a list of one. The process backend uses `.`. |
| `default_record_profile` | string | no | none | The [[record-profiles|record profile]] of a trial that has no `record_profile` factor and no explicit list of sources in `runner.yaml`. Without any of the three, the runner refuses the trial. |
| `parallel_safe_checks` | list of names | no | empty | The project's own [[gate-checks|gate checks]] that are read-only probes. The gate may run them in one batch with other parallel-safe checks. |
| `stop_patterns` | mapping | no | empty | What the stop sweeps also match. Its key `kinds` maps a kind name to a regular expression with a `{workspace}` placeholder. |
| `record_profiles` | mapping | no | empty | The names that fill the placeholders of `experiments/common/config/record_profiles.yaml`. An enabled robot adapter may bring defaults for some names, and this mapping wins over them. |
| `adapters` | list of names | no | empty | The [[robot-adapters|robot adapters]] of the backend that this project enables. The checks, the record profile blocks and the names of an adapter are in force only when the adapter is listed here. A single string counts as a list of one. |

The code reads no other key. It ignores a key that it does not know, so check the spelling of every key you add.

## Keys that only the ROS 2 backend reads

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `colcon_workspaces` | list of paths | `.` and `ros2_ws` | The older name of `build_workspaces`. It counts only when `build_workspaces` is absent. Without both keys, the ROS 2 backend uses `.` and `ros2_ws`. |
| `stop_patterns.recorder_scripts` | list of names | empty | Wrapper scripts that start `ros2 bag record`. The stop sweeps of the recorder and of the whole stack also match them. |
| `stop_patterns.owned_containers` | list of names | empty | Composable containers that the project's launch files start. The stop sweeps of the stack also match them. |
| `slim_topics.heavy_patterns` | list of strings | empty | More topic patterns that a slim copy of a bag leaves out. They add to the built-in patterns for image, depth and point cloud topics. |
| `slim_topics.light_suffixes` | list of strings | empty | Topic suffixes that a slim copy always keeps. They add to `/camera_info`. |

## Environment variables

These variables win over the file for one session.

| Variable | Replaces |
| --- | --- |
| `TRIALS_BACKEND` | `backend` |
| `TRIALS_EXPERIMENTS_ROOT` | `experiments_root` (the `--experiments-root` flag of a command wins over the variable) |
| `TRIALS_PROJECT_ROOT` | `project_root` (the `--project-root` flag of a command wins over the variable) |
| `TRIALS_STOP_LATCH` | `stop_latch` |

!!! note
    The settings come from the project whose experiments are in force. With `$TRIALS_EXPERIMENTS_ROOT` set, a command reads the `trials.yaml` at or above that directory, not the one around the working directory.
