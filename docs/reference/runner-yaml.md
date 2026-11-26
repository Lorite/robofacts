# runner.yaml

`runner.yaml` tells the [[runner]] how to run one trial of an experiment: how the stack starts, what it records, which command is the trial, and how the recording is checked. This page lists every key of the file.

## Where the file is

Each experiment has one, at `experiments/<name>/config/runner.yaml`. The runner owns the order of a trial. The file says only what is specific to the experiment.

## A minimal example

This example runs on the [[process-backend|process backend]]:

```yaml
simulation: true
launch:
  command: "python3 experiments/my_experiment/scripts/system.py --rate {rate}"
record_sources: [experiments/my_experiment/runs/system.log]
action:
  command: "python3 experiments/my_experiment/scripts/run_trial.py --rate {rate}"
verify_steps:
  - name: log_was_recorded
    command: "python3 experiments/my_experiment/scripts/verify_log.py {recording}"
hosts:
  laptop:
    local: true
```

## Keys

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `simulation` | boolean | yes (lint `L14`) | `false` | `true` only when every robot of the experiment is simulated, or there is none. Every trial records its domain from it. It must be a real boolean, not a string. |
| `launch` | mapping | depends on the backend | none | The stack. Its keys belong to the backend, see below. |
| `action` | mapping | no | none | `command`: the one foreground command that is the trial. |
| `record_sources` | list | no | empty | An explicit list of what to record. It wins over a record profile. `record_topics` is the same list under its older name. |
| `verify_steps` | list | yes, or `no_verify_steps` (lint `L6`) | empty | Checks of the recording after the recorder stopped, each `{name, command}`. A non-zero exit fails the trial, and the trial does not count. |
| `no_verify_steps` | string | instead of `verify_steps` | none | A sentence that says why no check of the recording is possible. |
| `setup_steps` | list | no | empty | Foreground commands that run in order before the stack starts, each `{name, command, required, when}`. |
| `aux_launches` | list | no | empty | Long-running helpers, each `{name, command}`. The runner starts them with the stack and stops them after the recorder and before the stack. |
| `metadata_probes` | list | no | empty | Commands that run on the trial's host after the trial, each `{key, command, json}`. The first line of the output goes into the metadata under `key`. A probe never fails a trial. |
| `settle_s` | number | no | `3.0` | Seconds between the start of the stack and the start of the recorder. |
| `stop_grace_s` | number | no | `2.0` | Seconds of grace when the runner stops the recorder and the stack. |
| `action_timeout_s` | number | no | none | A deadline for the action. A still running action receives SIGTERM, and the trial is aborted with the timeout as its reason. |
| `hosts` | mapping | for each level a trial uses | empty | One entry per level of the `execution_host` factor that this experiment may run on. A trial on a level that is not listed is refused. See [[robots-and-hosts]]. |
| `robots` | list | no | empty | The robots of [[robots-and-hosts|robots.yaml]] that this experiment uses. Their `reset` steps run before the stack starts, unless the campaign opts out. |
| `bags_root` | string | no | `repo` | Where the runner writes the recordings: `repo`, `external` (the destination of [[storage-yaml|storage.yaml]] for the recording host) or a path. A host entry may set its own. `--bags-root` wins over both. |
| `project_root` | path | no | the project root | The working directory of every command of this file. |
| `cf_name` | string | no | `record_profiles.cf_name` of `trials.yaml` | The name that fills the `{cf_name}` placeholder of the record profiles. A launch argument `cf_name` of the trial wins over it. |
| `livestream` | string or mapping | no | none | A viewer side launch, as a command or `{command}`. It starts only when a run turns its step on in the web interface. |

### The fields of a step

| Field | Used by | Default | Meaning |
| --- | --- | --- | --- |
| `name` | every list | `<list>_<index>` | The name in messages and in the step trace. |
| `command` | every list | none, required | The command line. |
| `required` | `setup_steps` | `true` | A required step that fails aborts the trial. An optional one only prints a warning. |
| `when` | `setup_steps` | `always` | `first` runs the step only on the first trial of a run, `repeat` only on the trials after it, `always` on every trial. |
| `key` | `metadata_probes` | none, required | The metadata field. It may not start with `_`. |
| `json` | `metadata_probes` | `false` | Parse the first output line as JSON. |

## Placeholders

The runner fills `{name}` placeholders from the trial. It splits a command into arguments first, so each value stays one argument. A metadata probe runs through `sh -c` instead, and the runner replaces only the placeholder names it knows, so other braces in a probe stay as they are.

| Placeholder | Available in |
| --- | --- |
| `{factor}`, one per factor | every command |
| `{arg_<argument>}`, one per launch argument | every command |
| `{recording}` and its older name `{bag}` | `action`, `aux_launches`, `verify_steps`, `metadata_probes` |

The `livestream` command takes no placeholders.

## Keys of `launch` by backend

| Backend | Keys | Meaning |
| --- | --- | --- |
| `process` | `command` | One command that runs until the runner stops it. It takes `{factor}` and `{arg_<argument>}`. Without `launch:` the experiment has no stack, and the action is the whole trial. |
| `ros2` | `package`, `file` (both required) | `ros2 launch <package> <file>`, with each launch argument as `name:=value`. |

## Keys that only the ROS 2 backend reads

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `launch_mode` | string | `launch` | `launch` runs `ros2 launch <package> <file>`. `run` runs `ros2 run <package> <executable>`, without launch arguments. |
| `record_storage` | string | `mcap` | The storage plugin of `ros2 bag record`. The process recorder ignores it. |
| `explore` | mapping | none | The settings of `trials explore`, see [[explore]]. On a real robot, Explore refuses an experiment without this block. |

The keys of `explore` are `param_whitelist` and `topic_whitelist` (lists of regular expressions), `port` (default `8765`), `address` (default `127.0.0.1`), `viewers` and `actions` (lists of `{name, command}`), and `jog` (a mapping with `topic`, `max_speed_mps`, `step_s`, `max_yaw_rate_rps` and `note`). The core loader refuses an unknown key in this block. In simulation, every parameter is open by default. On a real robot, lint rule `L16` refuses a pattern that matches every parameter.
